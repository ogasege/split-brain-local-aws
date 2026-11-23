output "bucket" {
  value = aws_s3_bucket.uploads.bucket
}

output "table" {
  value = aws_dynamodb_table.intervals.name
}

output "alert_queue_url" {
  value = aws_sqs_queue.alerts.url
}

output "dlq_url" {
  value = aws_sqs_queue.ingest_dlq.url
}

output "function" {
  value = aws_lambda_function.ingest.function_name
}

output "config_param" {
  value = aws_ssm_parameter.telemetry_config.name
}
