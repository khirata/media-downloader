# Simple Crontab Scheduling for Radiko Downloads

This guide explains how to use the `radiko-download.py` script and a standard Linux `crontab` to schedule your Radiko time-free recordings. 

This approach is simple and robust.

---

## 1. Setup

Ensure the script is executable and you have your `.env` configured.

```bash
cd radiko-downloader
chmod +x radiko-download.py
```

Your API credentials must be stored in a `.radiko-download.env` file. The script searches for this file in two places:
1. The exact same folder as the script
2. Your user config directory (`~/.config/.radiko-download.env`)

You can find the required API values in the `Outputs` section printed to the terminal after running `terraform apply` in the `api-gw` directory:

```ini
MEDIA_RECORDER_API_ENDPOINT="https://your-api-id.execute-api.us-west-2.amazonaws.com/prod/record"
MEDIA_RECORDER_API_KEY="your-api-key"
```

## 2. How the Script Works

The `radiko-download.py` script is designed to be run **after** a show has finished broadcasting.

**Arguments:**
- `--station`: The standard Radiko station ID (e.g., `FMJ`, `FMT`)
- `--desc`: (Optional) A description or name of the show
- `start_times`: One or more start times in full `YYYYMMDDHHMMSS` format (e.g., `20260314130000`)

## 3. Scheduling with Crontab

Configure your crontab using `crontab -e`.

**Example Crontab:**

```text
# Example: A 4-hour Sunday show (13:00 to 17:00). 
# We run the script at 17:05, right after it finishes.
# By calculating the current date dynamically, we can inject it directly into the parameter.
5 17 * * 0 d=$(TZ=Asia/Tokyo date -d 'now' +%Y%m%d) && ~/bin/radiko-download.py --station FMJ --desc "Sunday Long Show" ${d}130000 ${d}140000 ${d}150000 ${d}160000 >> ~/logs/radiko_cron.log 2>&1

```

## 4. Testing Manually

You can test the script manually at any time to verify it forms the correct URL and reaches the AWS API Gateway:

```bash
./radiko-download.py --station FMJ --desc "Test Download" 20260314150000
```
*(Check the script output to verify it generated the HTTP response 200 Success)*