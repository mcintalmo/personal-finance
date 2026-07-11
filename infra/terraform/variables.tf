# ============================================================
# variables.tf — personal-finance infrastructure variables
# ============================================================
# Define input variables here. Set values in terraform.tfvars
# (gitignored) or via environment variables (TF_VAR_<name>).
# ============================================================

variable "environment" {
  description = "Deployment environment (development | staging | production)"
  type        = string
  default     = "production"

  validation {
    condition     = contains(["development", "staging", "production"], var.environment)
    error_message = "environment must be one of: development, staging, production"
  }
}

variable "project_name" {
  description = "Project name, used as a prefix for resource names"
  type        = string
  default     = "personal-finance"
}

# Add more variables as needed:
# variable "aws_region" {
#   description = "AWS region to deploy into"
#   type        = string
#   default     = "us-east-1"
# }
