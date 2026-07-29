output "service_url" {
  value       = google_cloud_run_v2_service.lexora.uri
  description = "Set this as NEXT_PUBLIC_API_URL in Vercel."
}

output "service_account" {
  value = google_service_account.lexora.email
}
