# a Terraform file: blocks, which HCL keeps as blocks
variable "region" {
  default = "us-east-1"
}

locals {
  name = "demo-app"
}
