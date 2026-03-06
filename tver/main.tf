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

variable "sns_topic_arn" {
  description = "ARN of the dispatcher SNS topic"
  type        = string
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

# ==========================================
# Dead Letter Queue for TVer
# ==========================================
resource "aws_sqs_queue" "tver_dlq" {
  name = "${local.kebab_name}-tver-dlq"
}

# ==========================================
# TVer Queue & Routing
# ==========================================

# Main TVer Queue
resource "aws_sqs_queue" "tver_queue" {
  name                       = "${local.kebab_name}-tver"
  receive_wait_time_seconds  = 20
  visibility_timeout_seconds = 3600

  redrive_policy = jsonencode({
    deadLetterTargetArn = aws_sqs_queue.tver_dlq.arn
    maxReceiveCount     = 3 # Move to DLQ after 3 failed attempts
  })
}

# SNS Subscription with Filter Policy
resource "aws_sns_topic_subscription" "tver_subscription" {
  topic_arn = var.sns_topic_arn
  protocol  = "sqs"
  endpoint  = aws_sqs_queue.tver_queue.arn
  raw_message_delivery = true

  # ONLY accept messages where the JSON body 'type' is 'tver'
  filter_policy_scope = "MessageBody"
  filter_policy = jsonencode({
    type = ["tver"]
  })
}

resource "aws_sqs_queue_policy" "tver_queue_policy" {
  queue_url = aws_sqs_queue.tver_queue.id
  policy    = data.aws_iam_policy_document.sns_to_tver_sqs.json
}

data "aws_iam_policy_document" "sns_to_tver_sqs" {
  statement {
    effect = "Allow"
    principals {
      type        = "Service"
      identifiers = ["sns.amazonaws.com"]
    }
    actions   = ["sqs:SendMessage"]
    resources = [aws_sqs_queue.tver_queue.arn]
    condition {
      test     = "ArnEquals"
      variable = "aws:SourceArn"
      values   = [var.sns_topic_arn]
    }
  }
}

# ==========================================
# Worker Users and Credentials
# ==========================================

# TVer Worker credentials
resource "aws_iam_user" "tver_worker" {
  name = "${local.kebab_name}-tver-worker"
  path = "/"
}

resource "aws_iam_user_policy" "tver_worker_policy" {
  name = "${local.camel_name}TverWorkerPolicy"
  user = aws_iam_user.tver_worker.name

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
        Resource = aws_sqs_queue.tver_queue.arn
      }
    ]
  })
}

resource "aws_iam_access_key" "tver_key" {
  user = aws_iam_user.tver_worker.name
}

# ==========================================
# OUTPUTS
# ==========================================

output "tver_sqs_queue_url" {
  value       = aws_sqs_queue.tver_queue.url
  description = "URL of the TVer SQS queue"
}

output "tver_worker_access_key_id" {
  value       = aws_iam_access_key.tver_key.id
  description = "Access key for TVer worker"
}

output "tver_worker_secret_access_key" {
  value       = aws_iam_access_key.tver_key.secret
  description = "Secret key for TVer worker"
  sensitive   = true
}
