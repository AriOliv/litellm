output "cluster_name" {
  description = "GKE Autopilot cluster name; use with: gcloud container clusters get-credentials <name> --region <region>."
  value       = google_container_cluster.autopilot.name
}

output "gateway_static_ip" {
  description = "Reserved global static IP for the HTTPS load balancer. Point the gateway domain A record here at cutover."
  value       = google_compute_global_address.gateway.address
}

output "cloudsql_connection_name" {
  description = "Cloud SQL connection name (project:region:instance) for the Auth Proxy sidecar."
  value       = google_sql_database_instance.pg.connection_name
}

output "gateway_gsa_email" {
  description = "Google service account the gateway KSA impersonates. Annotate the KSA with this."
  value       = google_service_account.gateway.email
}
