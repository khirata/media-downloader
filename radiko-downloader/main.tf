provider "aws" {
  region = var.aws_region

  default_tags {
    tags = {
      Project = "media-downloader"
    }
  }
}

variable "aws_region" {
  description = "The AWS region to deploy resources in"
  type        = string
  default     = "us-west-2"
}

variable "base_name" {
  description = "Base name for resources"
  type        = string
  default     = "Media Downloader"
}

locals {
  kebab_name = replace(lower(var.base_name), " ", "-")
  camel_name = replace(title(var.base_name), " ", "")
}

variable "sns_topic_arn" {
  description = "ARN of the dispatcher SNS topic"
  type        = string
}

# ==========================================
# Radiko Queues & Routing
# ==========================================

# Main Radiko Queue
resource "aws_sqs_queue" "radiko_queue" {
  name                       = "${local.kebab_name}-radiko"
  receive_wait_time_seconds  = 20
  visibility_timeout_seconds = 3600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.radiko_dlq.arn
    maxReceiveCount     = 3 # Move to DLQ after 3 failed attempts
  })
}

# SNS Subscription with Filter Policy
resource "aws_sns_topic_subscription" "radiko_subscription" {
  topic_arn = var.sns_topic_arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.radiko_queue.arn
  raw_message_delivery = true

  # ONLY accept messages where the JSON body 'type' is 'radiko'
  filter_policy_scope = "MessageBody"
  filter_policy = jsonencode({
    type = ["radiko"]
  })
}

resource "aws_sqs_queue_policy" "radiko_queue_policy" {
  queue_url = aws_sqs_queue.radiko_queue.id
  policy    = data.aws_iam_policy_document.sns_to_radiko_sqs.json
}

data "aws_iam_policy_document" "sns_to_radiko_sqs" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.radiko_queue.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [var.sns_topic_arn]
    }
  }
}

# ==========================================
# Dead Letter Queue for Radiko
# ==========================================
resource "aws_sqs_queue" "radiko_dlq" {
  name = "${local.kebab_name}-radiko-dlq"
}


# ==========================================
# Worker Users and Credentials
# ==========================================

# Radiko Worker credentials
resource "aws_iam_user" "radiko_worker" {
  name = "${local.kebab_name}-radiko-worker"
  path = "/"
}

resource "aws_iam_user_policy" "radiko_worker_policy" {
  name = "${local.camel_name}RadikoWorkerPolicy"
  user = aws_iam_user.radiko_worker.name

  policy = jsonencode({
    Version = "2012-10-17"
    Statement = [
      {
        Effect = "Allow"
        Action = [
          "sqs:ReceiveMessage",
          "sqs:DeleteMessage",
          "sqs:GetQueueAttributes"
        ]
        Resource = aws_sqs_queue.radiko_queue.arn
      }
    ]
  })
}

resource "aws_iam_access_key" "radiko_key" {
  user = aws_iam_user.radiko_worker.name
}

output "radiko_sqs_queue_url" {
  value       = aws_sqs_queue.radiko_queue.url
  description = "URL of the Radiko SQS queue"
}

output "radiko_worker_access_key_id" {
  value       = aws_iam_access_key.radiko_key.id
  description = "Access key for Radiko worker"
}

output "radiko_worker_secret_access_key" {
  value       = aws_iam_access_key.radiko_key.secret
  description = "Secret key for Radiko worker"
  sensitive   = true
}
