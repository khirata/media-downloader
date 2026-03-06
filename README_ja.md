# サーバーレスメディアダウンローダー ([English](./README.md))

このリポジトリは、ストリーミングメディア（Radiko のラジオ番組や TVer の動画など）をダウンロードし、必要に応じて結合し、最終的にローカルに保存するか Google ドライブへ安全にアップロードするための、自動化されたイベント駆動型の録画・録音システムです。

複数のコンポーネントが連携するモノレポ構成となっています：

## 🗂️ プロジェクト構成

* **[chrome-extension](./chrome-extension/)**: ブラウザから URL をキャプチャし、API ゲートウェイに送信する Chrome 拡張機能。
* **[api-gw](./api-gw/)**: 着信リクエストを検証し、中央の AWS SNS トピックへ JSON ペイロードとしてディスパッチする AWS API Gateway と Lambda 関数。トラフィックルーターとして機能します（例: `radiko.jp` URL は Radiko SQS キューへ、`tver.jp` URL は TVer SQS キューへルーティング）。
* **[radiko](./radiko/)**: Radiko 専用の SQS キューを継続的にポーリングする Docker 化された Python ワーカ。`yt-dlp` でセグメントをダウンロードし、`ffmpeg` でシームレスに結合し、Google ドライブ API で最終的な `.m4a` ファイルをアップロードします。
* **[tver](./tver/)**: TVer 専用の SQS キューを継続的にポーリングする軽量な Docker 化された Python ワーカ。`yt-dlp` を使用して動画をローカルにダウンロードします。

## ⚙️ 要件

このプロジェクトを実行するには、以下のインフラストラクチャとツールが必要です：
* **Docker & Docker Compose** (ホストマシン、例: Ubuntu/Linux)
* **Terraform** (必要な AWS インフラストラクチャを自動的にプロビジョニングするため)
* **AWS アカウント** (SNS トピック、SQS キュー、IAM ユーザー用)
* **Google アカウント** (Radiko のオーディオファイルの保存先用。ホストにローカル保存する場合は不要です。7日ごとのトークン有効期限切れを避けるため、**Google Workspace** アカウントを推奨します。)
* **AWS CLI v2** (ホストマシンから手動で録音リクエストを送信するため)

---

## 🚀 セットアップ手順

### 1. AWS リソースのプロビジョニング (Terraform)
このプロジェクトは Terraform を使用して、必要な AWS SNS トピック、SQS キュー、および IAM ワーカの認証情報を自動的に作成します。

以下の3つのディレクトリで、**この順番通り**に Terraform を実行する必要があります：

1. **API Gateway (`api-gw/`)**: メインの SNS ディスパッチャートピックとパブリッシャー（発行者）認証情報を作成します。
2. **Radiko (`radiko/`)**: Radiko 用 SQS キューとワーカ認証情報を作成します。
3. **TVer (`tver/`)**: TVer 用 SQS キューとワーカ認証情報を作成します。

各ディレクトリで以下のコマンドを実行します：
```bash
cd [ディレクトリ名]
terraform init
terraform plan -var-file="../terraform.tfvars"
terraform apply -var-file="../terraform.tfvars"
cd ..
```
*(注: 適用する前に、提供されている `terraform.tfvars.example` を参考に `terraform.tfvars` を設定し、設定を中央管理（一元化）してください)。*

Terraform の実行が完了すると、必要な IAM アクセスキー、SQS キューの URL、および SNS トピックの ARN が出力されます。これらの値は、手順 3 の `.env` ファイル設定で使用するため控えておいてください。

### 2. Google Drive API の設定 (Radiko のみ)
Google Drive を設定しない場合 (手順3で `GDRIVE_FOLDER_ID` を空のままにした場合)、Radiko ワーカはアップロードを自動的にスキップし、代わりにホストマシンの `/tmp` ディレクトリに最終的な `.m4a` ファイルをローカル保存します。

Google Drive を使用したい場合：
1. Google Cloud Console にアクセスし、**Google Drive API** を有効にします。
2. **OAuth 同意画面** を作成します (Workspace ユーザーは内部(Internal)、一般ユーザーは外部(External)に設定)。
3. **OAuth クライアント ID** の認証情報 (デスクトップアプリ) を作成し、JSON をダウンロードします。
4. ローカルの認証スクリプトを実行して `token.json` ファイルを生成し、`radiko/` フォルダ内に配置します。*(注: `client_secret.json` は実行環境には含めないでください)。*

### 3. Docker 環境変数の設定
`radiko/` および `tver/` ディレクトリの**それぞれ**に `.env` ファイルを作成する必要があります。まずは example ファイルをコピーします：

```bash
cp radiko/.env.example radiko/.env
cp tver/.env.example tver/.env
```

2つの `.env` ファイルをそれぞれ編集し、新しくプロビジョニングした AWS 認証情報、該当する SQS キュー URL、および Google Drive フォルダ ID (該当する場合) を入力します。

### 4. ワーカのデプロイ
それぞれのディレクトリに移動して、ワーカを個別に開始できます：

**Radiko ワーカ:**
```bash
cd radiko
docker compose up -d --build
```

**TVer ワーカ:**
```bash
cd ../tver
docker compose up -d --build
```
これでコンテナはバックグラウンドで静かに動作し、それぞれの SQS キューに録画・録音タスクが届くのを待機します。

### 5. タスクのトリガー (手動リクエスト)
URL を配信する主要な方法は、Chrome 拡張機能経由で API ゲートウェイにリクエストを送ることですが、AWS CLI を使用して手動でタスクをトリガーすることも可能です。

`api-gw` 側の Terraform 出力で提供された `publisher` アクセスキーを使用して、ローカルの AWS CLI プロファイルを新規作成・設定します：
```bash
aws configure --profile media-downloader-publisher
```

**Radiko の例:**
```bash
aws sns publish \
  --profile media-downloader-publisher \
  --topic-arn "arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher" \
  --message "{\"type\": \"radiko\", \"station_id\": \"FMJ\", \"start_times\": [\"202602221300\", \"202602221400\"], \"description\": \"JUNK伊集院\"}"
```

**TVer の例:**
```bash
aws sns publish \
  --profile media-downloader-publisher \
  --topic-arn "arn:aws:sns:us-west-2:123456789012:media-downloader-dispatcher" \
  --message "{\"type\": \"tver\", \"url\": \"https://tver.jp/episodes/ex4mple\"}"
```

### 6. スケジューリング (Cron)
ラジオのレギュラー番組のように、定期的な自動録画・録音をおこなうには、システムの `crontab` を使用し、`aws sns` CLI からメッセージを自動発行します。手順 5 に記載されている AWS CLI コマンドを呼び出す一般的な cron ジョブを構築してください。

---

## 🎛️ yt-dlp のグローバル設定
このプロジェクトでは、環境変数を経由して `yt-dlp` にグローバルな引数を渡すことができます。

各ワーカの `.env` ファイルに `YT_DLP_ARGS` 変数を設定することで、そのワーカのすべてのダウンロードに共通して適用されるオプション（同時接続数、プロキシ、プレミアムアカウントの認証情報など）を指定できます。
```env
# Download Storage Configuration
# Set this to save media to a specific folder on your host machine.
# If left blank or commented out, downloads default to the host's /tmp directory.
DOWNLOAD_DIR=/path/to/your/custom/folder

YT_DLP_ARGS="-N 10 --extractor-args rajiko:premium_user=YOUR_USERNAME;premium_pass=YOUR_PASSWORD"
```
