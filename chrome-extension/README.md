# URL Publisher Chrome Extension

A Chrome Extension for quickly capturing URLs and publishing them as messages to an AWS SNS topic via an API Gateway. 

This project was built as a companion to the [Radiko Media Recorder](https://github.com/khirata/radiko-recorder/) project. It allows you to easily browse `radiko.jp` time-free URLs, stack them up in the extension popup, and batch-publish them to your Media Recorder's SNS dispatch topic. The backend Lambda function automatically parses Radiko URLs and formats them into the specific JSON format expected by the worker.

## Features
- Captures the active tab URL automatically.
- Stack multiple URLs and easily reorder or remove them.
- Batch-publish all stacked URLs to AWS.
- Backend Lambda intelligently parses and groups Radiko Time-Free URLs by station ID.

---

## 1. Set Up AWS Resources

The backend consists of an API Gateway securely guarded by an API Key, which proxies requests to a Node.js Lambda function that formats the URLs and publishes them to an SNS topic.

1. Ensure you have [Terraform](https://developer.hashicorp.com/terraform/downloads) and the AWS CLI installed.
2. Navigate to the `api-gw` directory:
   ```bash
   cd api-gw
   ```
3. Initialize Terraform:
   ```bash
   terraform init
   ```
4. Copy the example variables file:
   ```bash
   cp terraform.tfvars.example terraform.tfvars
   ```
5. Edit `terraform.tfvars`. You *must* specify the ARN of your existing SNS topic (e.g., the dispatcher topic from the Radiko Media Recorder setup). You can also configure the AWS region, project tag name, and an optional secret token.
6. Plan and apply the infrastructure:
   ```bash
   terraform apply
   ```
7. When the apply completes, Terraform will output two values: `api_endpoint` and `api_key`. Save these values; you will need them to configure the Chrome extension.

---

## 2. Deploy the Chrome Extension

Since this extension is not published to the Chrome Web Store, you will load it locally.

1. Open Google Chrome.
2. Navigate to the Chrome Extensions page by visiting `chrome://extensions/` in your address bar.
3. Turn on the **Developer mode** toggle in the top-right corner of the page.
4. Click the **Load unpacked** button in the top-left menu.
5. Select the `extension` folder located inside this project directory.
6. The "URL Publisher" extension should now appear in your list of extensions and in the toolbar puzzle-piece menu. (Pin it to your toolbar for easy access!)

---

## 3. Configure the Extension

Before you can publish URLs, you need to configure the extension to talk to your newly deployed AWS backend.

1. Click on the URL Publisher extension icon in your Chrome toolbar.
2. Click the gear icon (**⚙️ Settings**) in the top right corner of the popup.
3. Fill in the required fields using the outputs from step 1:
   - **API Gateway Endpoint URL**: Paste the `api_endpoint` URL.
   - **API Key**: Paste the `api_key` string.
   - **Custom Secret**: If you defined a `secret_token` string in `terraform.tfvars` (other than the default), enter it here.
4. Click **Save Settings**.
5. You are ready to go! Navigate to a Radiko time-free page, open the extension, and click **Publish**.
