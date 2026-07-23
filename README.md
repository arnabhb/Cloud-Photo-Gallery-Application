# AWS SQL and NoSQL Photo Gallery

A Flask photo gallery implemented twice to compare relational and NoSQL data models on AWS

- `sql/` stores users albums and photos in Amazon RDS for MySQL
- `nosql/` stores users in one DynamoDB table and album or photo records in another
- Both applications store image objects in Amazon S3 and can send account confirmation links through Amazon SES
- `template.yaml` provisions the shared AWS infrastructure with CloudFormation

## Features

- Account registration password hashing login logout and email verification
- Album creation deletion and search
- Photo upload deletion search tags and EXIF metadata display
- S3 object cleanup when photos albums or accounts are deleted
- Separate SQL and DynamoDB implementations with the same user interface

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
