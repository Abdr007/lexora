# ─────────────────────────────────────────────────────────────────────────────
# Lexora on Cloud Run — declarative, and sized to stay inside the always-free tier.
#
#   terraform init
#   terraform apply -var project_id=YOUR_PROJECT -var image=IMAGE_URL
#
# The free allowance is 2M requests and 360k GB-seconds per month. The knobs that
# decide whether this stays free are min_instances (0 — scale to zero, no idle
# billing), memory, and max_instances. They are set here, not left to defaults.
# ─────────────────────────────────────────────────────────────────────────────

terraform {
  required_version = ">= 1.6"
  required_providers {
    google = {
      source  = "hashicorp/google"
      version = "~> 6.0"
    }
  }
}

provider "google" {
  project = var.project_id
  region  = var.region
}

resource "google_project_service" "run" {
  service            = "run.googleapis.com"
  disable_on_destroy = false
}

resource "google_secret_manager_secret" "anthropic" {
  count     = var.anthropic_api_key == "" ? 0 : 1
  secret_id = "lexora-anthropic-api-key"
  replication {
    auto {}
  }
}

resource "google_secret_manager_secret_version" "anthropic" {
  count       = var.anthropic_api_key == "" ? 0 : 1
  secret      = google_secret_manager_secret.anthropic[0].id
  secret_data = var.anthropic_api_key
}

resource "google_service_account" "lexora" {
  account_id   = "lexora-api"
  display_name = "Lexora API"
}

resource "google_secret_manager_secret_iam_member" "anthropic_access" {
  count     = var.anthropic_api_key == "" ? 0 : 1
  secret_id = google_secret_manager_secret.anthropic[0].id
  role      = "roles/secretmanager.secretAccessor"
  member    = "serviceAccount:${google_service_account.lexora.email}"
}

resource "google_cloud_run_v2_service" "lexora" {
  name     = var.service_name
  location = var.region
  # Public read API with no user data and its own rate limiting.
  ingress = "INGRESS_TRAFFIC_ALL"

  template {
    service_account = google_service_account.lexora.email

    scaling {
      # Scale to zero: an idle instance burns the free GB-second allowance for
      # nothing. The trade is a cold start, which is why the models are baked
      # into the image rather than downloaded at boot.
      min_instance_count = 0
      max_instance_count = var.max_instances
    }

    containers {
      image = var.image

      resources {
        limits = {
          # The cross-encoder needs headroom; 2Gi is the smallest size that runs
          # it comfortably. Doubling memory doubles GB-second consumption, so this
          # is the number to watch against the free allowance.
          memory = "2Gi"
          cpu    = "2"
        }
        # Only bill for CPU while a request is in flight.
        cpu_idle = true
      }

      env {
        name  = "LEXORA_CORS_ALLOW_ORIGINS"
        value = var.cors_allow_origins
      }

      dynamic "env" {
        for_each = toset(var.anthropic_api_key == "" ? [] : ["anthropic"])
        content {
          name = "LEXORA_ANTHROPIC_API_KEY"
          value_source {
            secret_key_ref {
              secret  = google_secret_manager_secret.anthropic[0].secret_id
              version = "latest"
            }
          }
        }
      }

      ports {
        container_port = 7860
      }

      startup_probe {
        # The first request loads two ONNX sessions; give it room rather than
        # letting Cloud Run kill a container that is merely starting.
        initial_delay_seconds = 20
        timeout_seconds       = 5
        period_seconds        = 10
        failure_threshold     = 12
        http_get {
          path = "/api/health"
          port = 7860
        }
      }
    }

    timeout = "60s"
  }

  depends_on = [google_project_service.run]
}

resource "google_cloud_run_v2_service_iam_member" "public" {
  name     = google_cloud_run_v2_service.lexora.name
  location = google_cloud_run_v2_service.lexora.location
  role     = "roles/run.invoker"
  member   = "allUsers"
}
