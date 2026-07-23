# AWS SQL and NoSQL Photo Gallery

> **Generative AI disclosure**
>
> The baseline application templates for this project were created with generative AI assistance using **Cursor** and **ChatGPT**
>
> The generated starting points were reviewed, reorganized, documented, and modified to improve security consistency, maintainability, and repository readiness

## What this project does

This project is a cloud-based photo gallery application that lets users create accounts, verify their email addresses, create albums, upload photos, search stored content, and manage their own media

It includes two backend implementations of the same product

- A SQL version that stores users albums and photos in Amazon RDS for MySQL
- A NoSQL version that stores users albums and photos in Amazon DynamoDB

Both versions use Amazon S3 for image storage, Amazon SES for email verification, Flask for the backend, and Jinja templates with Bootstrap for the frontend

The project also includes AWS CloudFormation infrastructure that can provision the main cloud resources needed to run the application

Including both SQL and NoSQL implementations shows how one product can be designed for different storage strategies and helps demonstrate database selection as an architectural decision

## Why this project matters

Modern applications often need to combine user authentication, media storage, search metadata management, and reliable cloud infrastructure

This project demonstrates how the same photo gallery product can be implemented with both relational and NoSQL databases while sharing the same Flask interface and AWS services

The comparison is useful for real-world system design because it shows how database choice affects schema design, queries, deletion workflows, scalability, and application code

This project involves frontend, backend, database, and authentication systems

Practical applications include

- Media management platforms
- Digital asset libraries
- E-commerce product image systems
- Social and community applications
- Internal document and evidence repositories
- Cloud migration and database modernization projects

## Tech stack

| Layer | Technologies |
|---|---|
| Backend | Python Flask Jinja2 |
| Relational database | Amazon RDS for MySQL PyMySQL |
| NoSQL database | Amazon DynamoDB Boto3 |
| Object storage | Amazon S3 |
| Email verification | Amazon SES |
| Infrastructure as code | AWS CloudFormation |
| Authentication | Flask sessions Werkzeug password hashing timed verification tokens |
| Frontend | HTML Bootstrap CSS |
| Image metadata | EXIFRead |
| Deployment target | Amazon EC2 with an IAM instance role |

## Core functionality

- Account registration password hashing login logout and email verification
- Album creation deletion browsing and search
- Photo upload deletion browsing and search
- Photo tags descriptions timestamps and EXIF metadata
- S3 storage for photos and album thumbnails
- Automatic S3 cleanup when photos albums or accounts are deleted
- Parallel SQL and DynamoDB implementations using the same user-facing workflow
- CloudFormation provisioning for the supporting AWS infrastructure

## Architecture overview

The repository contains two implementations of the same application

- `sql/` stores users albums and photos in Amazon RDS for MySQL
- `nosql/` stores users in one DynamoDB table and album or photo records in another
- Both applications store image objects in Amazon S3 and send account confirmation links through Amazon SES
- `template.yaml` provisions the shared AWS infrastructure with CloudFormation

## Repository structure

```text
aws-photo-gallery/
├── template.yaml
├── requirements.txt
├── .env.example
├── database/schema.sql
├── static/styles.css
├── sql/
│   ├── app.py
│   └── templates/
└── nosql/
    ├── app.py
    └── templates/
```

## Local setup

```bash
python -m venv .venv
# Windows
.venv\Scripts\activate
# macOS or Linux
source .venv/bin/activate
pip install -r requirements.txt
```

Copy `.env.example` to `.env` and export the values through the shell or an environment loader

The applications use the standard AWS credential chain and do not contain access keys

For local development run `aws configure` or export temporary credentials

On EC2 use the IAM instance role created by the CloudFormation template

## Provision AWS resources

```bash
aws cloudformation deploy \
  --template-file template.yaml \
  --stack-name photo-gallery \
  --capabilities CAPABILITY_NAMED_IAM \
  --parameter-overrides \
    PhotoBucketName=your-unique-bucket-name \
    DBMasterPassword=your-database-password \
    KeyPairName=your-key-pair \
    SSHLocation=YOUR_PUBLIC_IP/32
```

Restrict `SSHLocation` instead of leaving SSH open to the internet

After deployment use the RDS endpoint output for `RDS_DB_HOSTNAME`

## Initialize the SQL database

```bash
mysql -h "$RDS_DB_HOSTNAME" -u photoapp -p photogallerydb < database/schema.sql
```

## Run the applications

```bash
python sql/app.py
python nosql/app.py
```

The SQL version listens on port `5000` and the NoSQL version listens on port `5001`

## Important deployment notes

- Amazon SES requires a verified sender and may require verified recipients while the account remains in the SES sandbox
- The S3 bucket is publicly readable because the gallery renders direct image URLs
- Production deployments should place the apps behind HTTPS and replace the development Flask server with Gunicorn or another WSGI server
- DynamoDB search uses scans because the original data model has no search index which is acceptable for a small demonstration but not ideal at scale
- Public album viewing is retained while album photo and account deletion are restricted to the owner

## Improvements made during repository cleanup

- Removed hard-coded AWS access keys email addresses IP addresses and generated-at-startup signing secrets
- Replaced missing local theme assets with self-contained Bootstrap templates
- Added stable environment-based configuration and a sample environment file
- Added the missing DynamoDB users table to CloudFormation
- Restricted MySQL ingress to the application security group
- Replaced broad IAM users with an EC2 instance role and scoped application permissions
- Added upload filename sanitization DynamoDB scan pagination and ownership checks
- Fixed SQL parameter tuples token validation connection handling and S3 deletion paths
