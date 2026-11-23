# Everything below goes through ONE aws provider (provider.gen.tf). Which
# emulator receives each call is decided by routing.toml, not here:
#   MiniStack    : S3 bucket, Lambda + role, SQS queues, SES identity, log group
#   AWS Emulator : DynamoDB table, SSM parameter

data "aws_caller_identity" "current" {}

locals {
  table_name = "${var.name_prefix}-meter-intervals"
  config_key = "/${var.name_prefix}/telemetry/config"
}

# ---------------------------------------------------------------- AWS Emulator

resource "aws_dynamodb_table" "intervals" {
  name         = local.table_name
  billing_mode = "PAY_PER_REQUEST"
  hash_key     = "pk" # "<site_id>#<meter_id>"
  range_key    = "sk" # interval end, ISO-8601 UTC

  attribute {
    name = "pk"
    type = "S"
  }
  attribute {
    name = "sk"
    type = "S"
  }
}

resource "aws_ssm_parameter" "telemetry_config" {
  name  = local.config_key
  type  = "String"
  value = jsonencode(var.telemetry_config)
}

# ---------------------------------------------------------------- MiniStack

resource "aws_s3_bucket" "uploads" {
  bucket        = "${var.name_prefix}-meter-uploads"
  force_destroy = true
}

resource "aws_sqs_queue" "alerts" {
  name                      = "${var.name_prefix}-meter-alerts"
  message_retention_seconds = 345600
}

resource "aws_sqs_queue" "ingest_dlq" {
  name = "${var.name_prefix}-ingest-dlq"
}

resource "aws_ses_email_identity" "alerts" {
  email = var.alert_email_from
}

resource "aws_iam_role" "ingest" {
  name = "${var.name_prefix}-ingest"
  assume_role_policy = jsonencode({
    Version = "2012-10-17"
    Statement = [{
      Effect    = "Allow"
      Principal = { Service = "lambda.amazonaws.com" }
      Action    = "sts:AssumeRole"
    }]
  })
}

# Neither emulator evaluates this policy. It is here so the same module is
# correct on a real account, which is where IAM must be tested.
resource "aws_iam_role_policy" "ingest" {
  name = "ingest"
  role = aws_iam_role.ingest.id
  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      { Effect = "Allow", Action = ["s3:GetObject"], Resource = "${aws_s3_bucket.uploads.arn}/incoming/*" },
      { Effect = "Allow", Action = ["dynamodb:PutItem"], Resource = aws_dynamodb_table.intervals.arn },
      { Effect = "Allow", Action = ["ssm:GetParameter"], Resource = aws_ssm_parameter.telemetry_config.arn },
      { Effect = "Allow", Action = ["sqs:SendMessage"], Resource = [aws_sqs_queue.alerts.arn, aws_sqs_queue.ingest_dlq.arn] },
      { Effect = "Allow", Action = ["ses:SendEmail"], Resource = aws_ses_email_identity.alerts.arn },
      { Effect = "Allow", Action = ["logs:CreateLogStream", "logs:PutLogEvents"], Resource = "${aws_cloudwatch_log_group.ingest.arn}:*" },
    ]
  })
}

resource "aws_cloudwatch_log_group" "ingest" {
  name              = "/aws/lambda/${var.name_prefix}-ingest"
  retention_in_days = 7
}

data "archive_file" "ingest" {
  type        = "zip"
  source_dir  = "${path.module}/../app"
  output_path = "${path.module}/../.local/ingest.zip"
  excludes    = ["**/__pycache__/**", "**/*.pyc"]
}

resource "aws_lambda_function" "ingest" {
  function_name    = "${var.name_prefix}-ingest"
  role             = aws_iam_role.ingest.arn
  runtime          = "python3.12"
  handler          = "ingest.handler.handler"
  filename         = data.archive_file.ingest.output_path
  source_code_hash = data.archive_file.ingest.output_base64sha256
  timeout          = 30
  memory_size      = 256

  dead_letter_config {
    target_arn = aws_sqs_queue.ingest_dlq.arn
  }

  environment {
    # routing_env comes from routing.toml: it points the function's own SDK
    # calls at the right emulator and trusts AWS Emulator's CA. On a real
    # account, drop it (set var-free) and the SDK uses normal AWS endpoints.
    variables = merge(local.routing_env, {
      TABLE_NAME       = aws_dynamodb_table.intervals.name
      ALERT_QUEUE_URL  = aws_sqs_queue.alerts.url
      CONFIG_PARAM     = aws_ssm_parameter.telemetry_config.name
      ALERT_EMAIL_FROM = aws_ses_email_identity.alerts.email
      ALERT_EMAIL_TO   = var.alert_email_to
    })
  }

  depends_on = [aws_cloudwatch_log_group.ingest]
}

resource "aws_lambda_function_event_invoke_config" "ingest" {
  function_name                = aws_lambda_function.ingest.function_name
  maximum_retry_attempts       = 2
  maximum_event_age_in_seconds = 3600 # set explicitly: MiniStack reports 21600 when unset, which caused a perpetual diff
}

resource "aws_lambda_permission" "s3_invoke" {
  statement_id   = "AllowS3Invoke"
  action         = "lambda:InvokeFunction"
  function_name  = aws_lambda_function.ingest.function_name
  principal      = "s3.amazonaws.com"
  source_arn     = aws_s3_bucket.uploads.arn
  source_account = data.aws_caller_identity.current.account_id
}

resource "aws_s3_bucket_notification" "uploads" {
  bucket = aws_s3_bucket.uploads.id

  lambda_function {
    lambda_function_arn = aws_lambda_function.ingest.arn
    events              = ["s3:ObjectCreated:*"]
    filter_prefix       = "incoming/"
    filter_suffix       = ".csv"
  }

  depends_on = [aws_lambda_permission.s3_invoke]
}
