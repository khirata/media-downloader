# Android Publisher

Android app that appears in the system share sheet and publishes shared URLs to the media-downloader API Gateway.

## Features

- Registers as a share target for `text/plain` (URLs from Chrome, TVer, YouTube, etc.)
- Optional description field
- Stores API endpoint and key in device SharedPreferences
- Supports radiko, TVer, and YouTube URLs (routing is handled server-side)

## Setup

### Requirements

- Android Studio (Hedgehog or later)
- Android device or emulator with API 26+

### Build & install

1. Open this directory in Android Studio
2. Wait for Gradle sync to complete
3. Connect your device via USB with USB debugging enabled
4. Run the app via the **▶** button — it installs and opens to Settings

### Configure

Enter your API Gateway invoke URL and API key, then tap **Save**:

| Field | Example |
|---|---|
| API Endpoint URL | `https://xxxxxx.execute-api.ap-northeast-1.amazonaws.com/prod/publish` |
| API Key | value from `terraform.tfvars` |

## Usage

1. Open Chrome (or any app) and navigate to a TVer, YouTube, or Radiko URL
2. Tap **Share → Media Downloader**
3. Optionally add a description, then tap **Publish**

## API

Sends a `POST` request matching the same schema as the Chrome extension:

```json
{
  "urls": ["https://tver.jp/episodes/..."],
  "description": "optional"
}
```

Header: `x-api-key: <your key>`

## Project structure

```
app/src/main/
├── AndroidManifest.xml         # share intent filter declaration
├── java/com/mediadownloader/publisher/
│   ├── ShareActivity.kt        # share sheet handler + API call
│   └── SettingsActivity.kt     # endpoint/key configuration
└── res/
    ├── layout/
    │   ├── activity_share.xml
    │   └── activity_settings.xml
    └── values/
        ├── strings.xml
        └── themes.xml
```
