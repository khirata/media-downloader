# サーバーレスメディアレコーダー (Radiko)

## 📖 プロジェクト概要
このプロジェクトは、ラジオ番組 (Radiko) やその他のメディアをダウンロードし、結合して Google ドライブへ安全にアップロードするための、自動化されたイベント駆動型の録音システムです。

重いローカルの cron ジョブに依存する代わりに、このアーキテクチャでは非常に疎結合な **AWS SNS -> SQS** パイプラインを使用します。軽量な Docker 化された Python ワーカがバックグラウンドで実行され、継続的にキューをポーリングします。放送局 ID と開始時刻を含む JSON ペイロードを受け取ると、`yt-dlp` を使用してセグメントをダウンロードし、`ffmpeg` でそれらをシームレスに結合し、最後に Google ドライブ API を使用して最終的な `.m4a` ファイルをアップロードします。ローカルのワークスペースはホストの `/tmp` ディレクトリにマッピングされているため、自動的にクリーンアップされ、ディスクを圧迫することはありません。

## ⚙️ 要件
このプロジェクトを実行するには、以下のインフラストラクチャとツールが必要です：
* **Docker & Docker Compose** (ホストマシン、例: Ubuntu/Linux)
* **Terraform** (必要な AWS インフラストラクチャを自動的にプロビジョニングするため)
* **AWS アカウント** (SNS トピック、SQS キュー、IAM ユーザー用)
* **Google アカウント** (Google Drive をオーディオファイルの保存先として使用する場合。使用せずにホストのローカルストレージに保存することも可能です。標準の `@gmail.com` でも動作しますが、**Google Workspace** アカウントを強く推奨します。下記の制限事項を参照してください。)
* **AWS CLI v2** (ホストマシンから手動で録音リクエストを送信するため)

---

## ⚠️ 重要: Google Workspace vs 通常の Google アカウント
Google Workspace アカウントのセットアップは**完全に任意**ですが、バックグラウンドでの自動化に標準の個人の Google アカウント (`@gmail.com`) を使用する場合の制限事項を理解しておく必要があります。

**Google Workspace アカウントを使用する場合 (推奨):**
Google Cloud の OAuth アプリを **"内部 (Internal)"** アプリケーションとして設定できます。組織の内部用であるため、Google はこれを完全に信頼します。生成した `token.json` (リフレッシュトークン) は**無期限**になります。バックグラウンドワーカは、放置したままで数年間実行し続けることができます。

**通常の Google アカウント (`@gmail.com`) を使用する場合:**
OAuth アプリを **"外部 (External)"** アプリケーションとして設定する必要があります。個人のスクリプトを Google の正式なセキュリティ監査に提出することはないため、アプリは **"テスト"** モードのままである必要があります。
* **制限:** Google は、テストモードの外部アプリに対してリフレッシュトークンの**有効期限を厳格に 7 日間**に制限しています。
* **結果:** 個人アカウントを使用した場合、バックグラウンドワーカはセットアップからちょうど1週間で動作しなくなります。7日ごとに手動でローカルの認証スクリプトを実行し、新しい `token.json` ファイルを生成する必要があります。

---

## 🚀 セットアップ手順

### 1. AWS リソースのプロビジョニング (Terraform)
このプロジェクトは Terraform を使用して、必要な AWS SNS トピック、SQS キュー、および IAM ワーカの認証情報を自動的に作成します。

1. [Terraform](https://developer.hashicorp.com/terraform/downloads) がインストールされており、AWS CLI に管理者権限が設定されている (`aws configure`) ことを確認します。
2. Terraform ワークスペースを初期化します：
   ```bash
   terraform init
   ```
3. インフラストラクチャを確認して適用します：
   ```bash
   terraform plan
   terraform apply
   ```
   *(注: 既存のリソースを更新したり後で新しいキューを追加したりするには、単に `main.tf` を変更してもう一度 `terraform apply` を実行します)。*
4. Terraform は、必要な IAM アクセスキー、SQS キュー URL、SNS トピック ARN を出力します。これらの値は `.env` ファイルの設定で使用するため、控えておいてください。

### 2. Google Drive API の設定 (任意)
Google Drive を設定しない場合 (手順3で `GDRIVE_FOLDER_ID` を空のままにした場合)、アップロードは自動的にスキップされ、代わりにホストマシンの `/tmp` ディレクトリに最終的な `.m4a` ファイルがローカル保存されます。

Google Drive を使用したい場合：
1. Google Cloud Console にアクセスします。
2. **Google Drive API** を有効にします。
3. **OAuth 同意画面**を作成します：
   * *Workspace ユーザー:* ユーザーの種類を **内部 (Internal)** に設定します。
   * *通常のユーザー:* ユーザーの種類を **外部 (External)** に設定し、公開ステータスは **テスト** のままにします。
4. **OAuth クライアント ID** の認証情報 (デスクトップアプリ) を作成し、JSON をダウンロードします。
5. ローカルの認証スクリプトを実行して `token.json` ファイルを生成します。`token.json` は本プロジェクトのルートディレクトリに配置してください。*(注: `client_secret.json` は実行環境には含めないでください)。*

### 3. 環境変数の設定
プロジェクトルートに `../.env` ファイルを作成する (またはホスト環境で設定する) ことで、`docker-compose.yml` にあわせた設定を行います。AWS の値には Terraform の設定出力を使用します：
```env
RADIKO_SQS_QUEUE_URL=https://sqs.us-west-2.amazonaws.com/123456789012/media-downloader-radiko
TVER_SQS_QUEUE_URL=https://sqs.us-west-2.amazonaws.com/123456789012/media-downloader-tver
AWS_ACCESS_KEY_ID=your_terraform_radiko_access_key
AWS_SECRET_ACCESS_KEY=your_terraform_radiko_secret_key
AWS_REGION=us-west-2

# 任意: グローバルな yt-dlp 引数
# YT_DLP_ARGS="--extractor-args rajiko:premium_user=USER;premium_pass=PASS"

# Google Drive 設定 (URLからのフォルダID)
# ファイルをホストの /tmp ディレクトリにローカル保存したい場合は、これを空のままにしてください！
GDRIVE_FOLDER_ID=your_google_drive_folder_id
```

### 4. ワーカのデプロイ
次のコマンドを実行して Python 3.11 イメージをビルドし、バックグラウンドのポーリングサービスを開始します：
```bash
docker compose up -d --build
```
コンテナはバックグラウンドで静かに動作し、SQS キューにメッセージが届くのを待機します。

### 5. 録音のトリガー (リクエスト送信)
録音をリクエストするには、JSON メッセージを AWS SNS トピックに発行 (Publish) します。ワーカのコンテナは最小権限 (メッセージの読み取りと削除のみ可能) で実行されるため、この操作には専用の `publisher` の認証情報を使用する必要があります。

Terraform の出力で提供される `publisher` アクセスキーを使用して、ローカルの AWS CLI プロファイルを新規作成します。プロンプトが表示されたら、Default region name (デフォルトリージョン名) にリソースをデプロイしたリージョン (例: `us-west-2`) を設定するようにしてください。
```bash
aws configure --profile media-downloader-publisher
```

作成したプロファイルを使用した AWS CLI コマンドの例 (2つの1時間セグメントを結合する場合) :
```bash
aws sns publish \
  --profile media-downloader-publisher \
  --topic-arn "arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher" \
  --message "{\"type\": \"radiko\", \"station_id\": \"FMJ\", \"start_times\": [\"202602221300\", \"202602221400\"], \"description\": \"JUNK伊集院\"}"
```

### 6. 録音のスケジューリング (Cron)
システムの `crontab` を使用して `aws sns` CLI からメッセージを自動発行することで、定期的な録音をスケジュールできます。

`crontab` の先頭に `MEDIA_DOWNLOADER_SNS` と `MEDIA_DOWNLOADER_PROFILE` という変数を定義します。
* **`MEDIA_DOWNLOADER_SNS`**: Terraform の出力 (`sns_topic_arn`) で提供される SNS トピック ARN です。
* **`MEDIA_DOWNLOADER_PROFILE`**: 手順 5 で設定したローカルの AWS プロファイル名です (例: `media-downloader-publisher`)。

```crontab
# 使用する AWS プロファイルと SNS トピック ARN を定義します
MEDIA_DOWNLOADER_PROFILE="media-downloader-publisher"
MEDIA_DOWNLOADER_SNS="arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher"

# 例: 毎週日曜日の 15:00 に録音を実行 (13:00 と 14:00 の2つのセグメントを結合)
# 注意: aws CLI のパスがシステムで正しいことを確認してください (例: /usr/local/bin/aws または /usr/bin/aws)
0 15 * * 0 /usr/local/bin/aws sns publish --profile $MEDIA_DOWNLOADER_PROFILE --topic-arn $MEDIA_DOWNLOADER_SNS --message "{\"type\": \"radiko\", \"station_id\": \"FMJ\", \"start_times\": [\"$(date +\%Y\%m\%d)1300\", \"$(date +\%Y\%m\%d)1400\"]}"
```
*(注釈: cron では `%` は改行として扱われるため、crontab に記述する際は `\%` のようにエスケープする必要があります).*

---

## 🎛️ yt-dlp の設定方法
このプロジェクトでは、`yt-dlp` (および `yt-dlp-rajiko` などのエクストラクタ) に引数を渡すための柔軟な3つの方法をサポートしています。

### 1. グローバル環境変数
`../.env` ファイルに `YT_DLP_ARGS` 変数を設定することで、すべての録音に適用されるグローバルなオプション（同時接続数、プロキシ、プレミアムアカウントの認証情報など）を指定できます。
```env
YT_DLP_ARGS="-N 10 --extractor-args rajiko:premium_user=YOUR_USERNAME;premium_pass=YOUR_PASSWORD"
```

### 2. SQS メッセージによるオーバーライド (個別指定)
一時的な調整のために、SNS/SQS メッセージを発行する際の JSON ペイロードに `yt_dlp_args` 配列を含めることができます。これらの引数は、グローバル環境変数の**後**に追加されます。
```bash
aws sns publish ... --message "{\"station_id\": \"FMJ\", \"start_times\": [\"...\"], \"yt_dlp_args\": [\"--limit-rate\", \"1M\"]}"
```

### 3. ネイティブ設定ファイル (`yt-dlp.conf`)
コンテナ内の `yt-dlp` プロセスは `--ignore-config` を使用しなくなりました。これにより、専用の設定ファイルを管理したい場合は、`docker-compose.yml` を介して標準の `yt-dlp.conf` ファイルをコンテナ内の `/etc/yt-dlp.conf` にマウントすることができます。
