# Radiko ダウンロードスクリプトとスケジューリング ([English](./SCHEDULING.md))

このガイドでは、`radiko-download.py` スクリプトと標準的な Linux の `crontab` を使用して、Radiko のタイムフリー録音をスケジュールする方法を説明します。

このアプローチはシンプルで堅牢です。

---

## 1. セットアップ

スクリプトに実行権限を付与してください：

```bash
cd radiko-downloader
chmod +x radiko-download.py
```

API 認証情報は `.radiko-download.env` ファイルに保存する必要があります。スクリプトは以下の2か所からこのファイルを検索します：
1. スクリプトと同じフォルダ
2. ユーザー設定ディレクトリ（`~/.config/.radiko-download.env`）

必要な API の値は、`api-gw` ディレクトリで `terraform apply` を実行した後、ターミナルに表示される `Outputs` セクションで確認できます：

```ini
MEDIA_RECORDER_API_ENDPOINT="https://your-api-id.execute-api.us-west-2.amazonaws.com/prod/record"
MEDIA_RECORDER_API_KEY="your-api-key"
```

## 2. スクリプトの動作

`radiko-download.py` スクリプトは、番組の放送が**終了した後**に実行するように設計されています。

**引数：**
- `--station`：Radiko の標準的なステーション ID（例：`FMJ`、`FMT`）
- `--desc`：（任意）番組の説明や名前
- `start_times`：`YYYYMMDDHHMMSS` 形式の開始時刻を1つ以上（例：`20260314130000`）

## 3. Crontab でのスケジューリング

`crontab -e` を使用して crontab を設定します。

**Crontab の例：**

```text
# 例：日曜日の4時間番組（13:00〜17:00）
# 番組終了直後の 17:05 にスクリプトを実行します。
# 現在の日付を動的に計算することで、パラメータに直接注入できます。
5 17 * * 0 d=$(TZ=Asia/Tokyo date -d 'now' +%Y%m%d) && ~/bin/radiko-download.py --station FMJ --desc "Sunday Long Show" ${d}130000 ${d}140000 ${d}150000 ${d}160000 >> ~/logs/radiko_cron.log 2>&1

```

## 4. 手動テスト

スクリプトが正しい URL を生成し、AWS API Gateway に到達できることを確認するため、いつでも手動でテストできます：

```bash
./radiko-download.py --station FMJ --desc "Test Download" 20260314150000
```
*（スクリプトの出力を確認して、HTTP レスポンス 200 Success が返ることを確認してください）*