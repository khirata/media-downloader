# Serverless Media Downloader (Radiko)

## 📖 Project Description
This is an automated, event-driven media recording system designed to download radio programs (Radiko) and other media, stitch them together, and upload them securely to Google Drive. 

Instead of relying on heavy local cron jobs, this architecture uses a highly decoupled **AWS SNS -> SQS** pipeline. A lightweight Dockerized Python worker runs in the background, continuously polling the queue. When it receives a JSON payload containing the station ID and start times, it uses `yt-dlp` to download the segments, `ffmpeg` to concatenate them seamlessly, and the Google Drive API to upload the final `.m4a` file. The local workspace is mapped to the host's `/tmp` directory, ensuring automatic cleanup and zero disk bloat.

## ⚙️ Requirements
To run this project, you need the following infrastructure and tools:
* **Docker & Docker Compose** (Host machine, e.g., Ubuntu/Linux)
* **Terraform** (To automatically provision the required AWS infrastructure)
* **AWS Account** (For SNS Topics, SQS Queues, and IAM Users)
* **Google Account** (Destination for saving audio files. You can also skip this and save to the host's local storage instead.A standard `@gmail.com` works, but a **Google Workspace** account is highly recommended. See limitations below.)
* **AWS CLI v2** (For sending manual recording requests from the host machine)

---

## ⚠️ Important: Google Workspace vs. Regular Google Account
Setting up a Google Workspace account is **completely optional**, but you must understand the limitations of using a standard personal Google account (`@gmail.com`) for background automation.

**If you use a Google Workspace Account (Recommended):**
You can configure your Google Cloud OAuth app as an **"Internal"** application. Because it is internal to your organization, Google trusts it completely. The `token.json` refresh token you generate will **never expire**. Your background worker can run unattended for years.

**If you use a Regular Google Account (`@gmail.com`):**
You are forced to configure your OAuth app as an **"External"** application. Because you are not going to submit this personal script to Google for a formal security audit, the app must remain in **"Testing"** mode. 
* **The Limitation:** Google strictly enforces a **7-day expiration limit** on refresh tokens for External apps in Testing mode. 
* **The Result:** If you use a personal account, your background worker will break exactly one week after you set it up. You will have to manually run the local authentication script to generate a new `token.json` file every 7 days.

---

## 🚀 Setup Instructions

### 1. Provision AWS Resources (Terraform)
This project uses Terraform to automate the creation of the required AWS SNS Topics, SQS Queues, and IAM Worker credentials.

1. Ensure you have [Terraform](https://developer.hashicorp.com/terraform/downloads) installed and your AWS CLI configured with admin permissions (`aws configure`).
2. Initialize the Terraform workspace:
   ```bash
   terraform init
   ```
3. Review and apply the infrastructure:
   ```bash
   terraform plan
   terraform apply
   ```
   *(Note: To update existing resources or add new queues later, simply modify `main.tf` and run `terraform apply` again).*
4. Terraform will output the necessary IAM access keys, SQS Queue URLs, and the SNS Topic ARN. Keep these values handy for the `.env` file configuration.

### 2. Google Drive API Configuration (Optional)
If you do **not** configure Google Drive (by leaving `GDRIVE_FOLDER_ID` empty in step 3), the worker will automatically skip uploading and instead save the final `.m4a` files locally to your host machine's `/tmp` directory.

If you want to use Google Drive:
1. Go to the Google Cloud Console.
2. Enable the **Google Drive API**.
3. Create an **OAuth Consent Screen**:
   * *Workspace users:* Set User Type to **Internal**.
   * *Regular users:* Set User Type to **External** and leave the publishing status as **Testing**.
4. Create **OAuth Client ID** credentials (Desktop App) and download the JSON.
5. Run the local authentication script to generate your `token.json` file. Place `token.json` in the root of this project. *(Note: Do not include `client_secret.json` in the runtime environment).*

### 3. Configure Environment Variables
Create a `../.env` file in the project root (or set these in your host environment) matching your `docker-compose.yml`. Use the outputs from the Terraform configuration for the AWS values:
```env
SQS_QUEUE_URL=https://sqs.us-west-2.amazonaws.com/123456789012/media-downloader-radiko
AWS_ACCESS_KEY_ID=your_terraform_radiko_access_key
AWS_SECRET_ACCESS_KEY=your_terraform_radiko_secret_key
AWS_REGION=us-west-2

# Optional: Global yt-dlp arguments
# YT_DLP_ARGS="--extractor-args rajiko:premium_user=USER;premium_pass=PASS"

# Google Drive Configuration (Folder ID from URL)
# Leave this blank if you prefer to save files locally to your host's /tmp directory!
GDRIVE_FOLDER_ID=your_google_drive_folder_id
```

### 4. Deploy the Worker
Run the following command to build the Python 3.11 image and start the background polling service:
```bash
docker compose up -d --build
```
The container will now run silently, waiting for messages in the SQS queue.

### 5. Trigger a Recording
To request a recording, publish a JSON message to your AWS SNS topic. Since the worker containers operate with least-privilege permissions (they can only read/delete messages), you must use the dedicated `publisher` credentials for this.

Configure a new local AWS profile using the `publisher` access keys provided by the Terraform output. When prompted, make sure to set the Default region name to the region where you deployed your resources (e.g., `us-west-2`):
```bash
aws configure --profile media-downloader-publisher
```

Example AWS CLI command (concatenating two 1-hour segments), using the created profile:
```bash
aws sns publish \
  --profile media-downloader-publisher \
  --topic-arn "arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher" \
  --message "{\"type\": \"radiko\", \"station_id\": \"FMJ\", \"start_times\": [\"202602221300\", \"202602221400\"], \"description\": \"JUNK伊集院\"}"
```

### 6. Scheduling Recordings (Cron)
You can schedule recurring recordings using your system's `crontab` by automatically publishing messages via the `aws sns` CLI. 

Define the variables `MEDIA_DOWNLOADER_SNS` and `MEDIA_DOWNLOADER_PROFILE` at the top of your crontab. 
* **`MEDIA_DOWNLOADER_SNS`**: The SNS Topic ARN provided by the Terraform output (`sns_topic_arn`).
* **`MEDIA_DOWNLOADER_PROFILE`**: The local AWS profile name you configured in Step 5 (e.g., `media-downloader-publisher`).

```crontab
# Define your AWS profile and SNS topic ARN
MEDIA_DOWNLOADER_PROFILE=media-downloader-publisher
MEDIA_DOWNLOADER_SNS=arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher

# Example: Record a weekly show every Sunday at 15:00. This example requests 13:00 and 14:00 segments.
# Note: Ensure the aws cli path is correct for your system (e.g. /usr/local/bin/aws or /usr/bin/aws)
0 15 * * 0 /usr/local/bin/aws sns publish --profile $MEDIA_DOWNLOADER_PROFILE --topic-arn $MEDIA_DOWNLOADER_SNS --message "{\"type\": \"radiko\", \"station_id\": \"FMJ\", \"start_times\": [\"$(date +\%Y\%m\%d)1300\", \"$(date +\%Y\%m\%d)1400\"]}"
```
*(Note: Cron treats `%` as a newline character, so you must escape them as `\%` in your crontab).*

---

## 🎛️ yt-dlp Configuration
This project supports three flexible ways to pass arguments to `yt-dlp` (and its extractors like `yt-dlp-rajiko`).

### 1. Global Environment Variable
You can set a `YT_DLP_ARGS` variable in your `../.env` file to apply global options (like concurrent connections, proxies, or premium account credentials) to all recordings.
```env
YT_DLP_ARGS="-N 10 --extractor-args rajiko:premium_user=YOUR_USERNAME;premium_pass=YOUR_PASSWORD"
```

### 2. SQS Message Override
For one-off adjustments, you can include a `yt_dlp_args` array in your JSON payload when publishing the SNS/SQS message. These arguments will be appended *after* the global environment variables.
```bash
aws sns publish ... --message "{\"station_id\": \"FMJ\", \"start_times\": [\"...\"], \"yt_dlp_args\": [\"--limit-rate\", \"1M\"]}"
```

### 3. Native Configuration File
The `yt-dlp` process inside the container no longer uses `--ignore-config`. This means you can mount a standard `yt-dlp.conf` file into the container at `/etc/yt-dlp.conf` via `docker-compose.yml` if you prefer managing a dedicated configuration file.
