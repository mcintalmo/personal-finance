# ============================================================
# main.tf — personal-finance infrastructure
# ============================================================
# Fill in your cloud provider and resources below.
# Recommended: pin provider versions in versions.tf.
#
# Common starting points:
#   AWS:    https://registry.terraform.io/providers/hashicorp/aws
#   GCP:    https://registry.terraform.io/providers/hashicorp/google
#   Azure:  https://registry.terraform.io/providers/hashicorp/azurerm
#   Fly.io: https://registry.terraform.io/providers/fly-apps/fly
# ============================================================

terraform {
  required_version = ">= 1.9"

  # Uncomment to store state in a remote backend (recommended for teams):
  # backend "s3" {
  #   bucket = "personal-finance-terraform-state"
  #   key    = "terraform.tfstate"
  #   region = "us-east-1"
  # }

  required_providers {
    # Add your provider here, e.g.:
    # aws = {
    #   source  = "hashicorp/aws"
    #   version = "~> 5.0"
    # }
  }
}

# Configure the provider:
# provider "aws" {
#   region = var.aws_region
# }

# Add resources below:
