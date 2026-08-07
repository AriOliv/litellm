locals {
  services = [
    "container.googleapis.com",
    "sqladmin.googleapis.com",
    "secretmanager.googleapis.com",
    "certificatemanager.googleapis.com",
    "artifactregistry.googleapis.com",
    "compute.googleapis.com",
  ]

  gsa_id    = "litellm-gateway"
  wi_member = "serviceAccount:${var.project_id}.svc.id.goog[${var.app_namespace}/${var.gateway_ksa}]"
}

resource "google_project_service" "enabled" {
  for_each                   = toset(local.services)
  service                    = each.value
  disable_dependent_services = false
  disable_on_destroy         = false
}

# Immutable image storage for the gateway. Skip by setting artifact_repo = "" if reusing gcr.io.
resource "google_artifact_registry_repository" "images" {
  count         = var.artifact_repo == "" ? 0 : 1
  location      = var.region
  repository_id = var.artifact_repo
  format        = "DOCKER"
  depends_on    = [google_project_service.enabled]
}

# Global static IP for the external HTTPS load balancer fronting the Gateway API.
resource "google_compute_global_address" "gateway" {
  name       = var.static_ip_name
  depends_on = [google_project_service.enabled]
}

resource "google_container_cluster" "autopilot" {
  name             = var.cluster_name
  location         = var.region
  enable_autopilot = true

  # Autopilot manages nodes; releases follow the regular channel.
  release_channel {
    channel = "REGULAR"
  }

  # Workload Identity is on by default under Autopilot; declared for clarity.
  workload_identity_config {
    workload_pool = "${var.project_id}.svc.id.goog"
  }

  deletion_protection = var.deletion_protection
  depends_on          = [google_project_service.enabled]
}

# Cloud SQL for PostgreSQL 16, regional HA, storage autogrow (kills the disk-full failure mode).
resource "google_sql_database_instance" "pg" {
  name                = var.db_instance_name
  region              = var.region
  database_version    = "POSTGRES_16"
  deletion_protection = var.deletion_protection

  settings {
    tier              = var.db_tier
    availability_type = "REGIONAL"
    disk_type         = "PD_SSD"
    disk_size         = var.db_disk_size_gb
    disk_autoresize   = true

    backup_configuration {
      enabled                        = true
      point_in_time_recovery_enabled = true
    }

    ip_configuration {
      # Private path is preferred in production; the Cloud SQL Auth Proxy sidecar
      # connects over IAM regardless. Configure a private network here if the
      # cluster runs on a VPC with private services access.
      ipv4_enabled = true
    }
  }

  depends_on = [google_project_service.enabled]
}

resource "google_sql_database" "app" {
  name     = var.db_name
  instance = google_sql_database_instance.pg.name
}

# The DB user password is generated and stored in Secret Manager; never printed or committed.
resource "random_password" "db" {
  length  = 32
  special = false
}

resource "google_sql_user" "app" {
  name     = var.db_user
  instance = google_sql_database_instance.pg.name
  password = random_password.db.result
}

resource "google_secret_manager_secret" "db_password" {
  secret_id = "litellm-db-password"
  replication {
    auto {}
  }
  depends_on = [google_project_service.enabled]
}

resource "google_secret_manager_secret_version" "db_password" {
  secret      = google_secret_manager_secret.db_password.id
  secret_data = random_password.db.result
}

# Google service account the gateway pods impersonate via Workload Identity.
resource "google_service_account" "gateway" {
  account_id   = local.gsa_id
  display_name = "LiteLLM gateway (GKE Workload Identity)"
}

resource "google_service_account_iam_member" "wi_bind" {
  service_account_id = google_service_account.gateway.name
  role               = "roles/iam.workloadIdentityUser"
  member             = local.wi_member
}

resource "google_project_iam_member" "cloudsql_client" {
  project = var.project_id
  role    = "roles/cloudsql.client"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}

resource "google_project_iam_member" "secret_accessor" {
  project = var.project_id
  role    = "roles/secretmanager.secretAccessor"
  member  = "serviceAccount:${google_service_account.gateway.email}"
}
