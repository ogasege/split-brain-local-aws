variable "name_prefix" {
  description = "Prefix for every resource name."
  type        = string
  default     = "split-brain"
}

variable "telemetry_config" {
  description = "Meter thresholds stored in SSM Parameter Store and read by the ingest Lambda at cold start."
  type = object({
    register_max_kwh    = number
    rollover_window     = number
    max_interval_kwh    = number
    expected_interval_s = number
  })
  default = {
    register_max_kwh    = 99999.99
    rollover_window     = 0.05
    max_interval_kwh    = 50
    expected_interval_s = 900
  }
}

variable "alert_email_from" {
  description = "Verified SES sender for the alert digest."
  type        = string
  default     = "meter-alerts@example.com"
}

variable "alert_email_to" {
  description = "Site operations mailbox that receives the alert digest."
  type        = string
  default     = "site-ops@example.com"
}
