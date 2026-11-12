# Decision Table Experiment Tool

要求仕様から

1. 1つの抽出モデルで **Decision Items** を抽出
2. 同じ Requirements と同じ Decision Items を **複数の生成モデル** へ渡して **Decision Table** を生成

を一度の「実行」でまとめて行う、研究実験用の Windows デスクトップアプリ (PySide6) です。
LLM の呼び出しはすべて OpenRouter API 経由で行います。

生成結果は「比較」タブで、同じ条件の組み合わせに対してモデルごとの結果が
食い違っていないかを自動検出・ハイライト表示できます (詳細は後述)。

---

## 1. Python 環境の準備

Python 3.10 以上 (開発確認は 3.13) が必要です。

```powershell
cd C:\Users\yousu\research\tool\tool
python -m venv .venv
.\.venv\Scripts\Activate.ps1
```

## 2. 必要パッケージのインストール

```powershell
pip install -r requirements.txt
```

インストールされるもの: `PySide6` (GUI) / `requests` (HTTP) / `python-dotenv` (.env 読み込み)

## 3. OpenRouter API キーの設定

[https://openrouter.ai/keys](https://openrouter.ai/keys) でキーを発行し、
プロジェクト直下の `.env` に書きます (`.env.example` がひな形です)。

```
OPENROUTER_API_KEY=sk-or-v1-xxxxxxxx
```

キーが未設定のまま実行すると、その旨のエラーダイアログが出ます。
`.env` は Git 管理対象外です (`.gitignore`)。

> Windows のシステム環境変数に `OPENROUTER_API_KEY` を設定している場合は、そちらが優先されます
> (`.env` は既存の環境変数を上書きしません)。

## 4. アプリの起動

```powershell
python main.py
```

ブラウザは使いません。通常の Windows デスクトップアプリとして起動します。

---

## 5. OpenRouter からのモデル取得

- 起動時にバックグラウンドでモデル一覧を自動取得します (GUI は固まりません)。
- 明示的に取り直す場合は **モデル設定... → 「モデル一覧を更新」**。
- 取得した一覧は `config/models_cache.json` にキャッシュされ、次回起動直後の表示に使われます。
- 取得に失敗した場合はエラーメッセージを表示し、キャッシュがあればそれを使って動作を続けます
  (アプリは落ちません)。
- モデル一覧はコードにハードコードしていません。

## 6. 抽出モデル・生成モデルの設定

メイン画面の **「モデル設定...」** を押すと設定ダイアログが開きます。

- 左: **抽出モデル** — Decision Items 抽出に使うモデルを 1 つ選択
- 右: **生成モデル** — Decision Table 生成に使うモデルをチェックボックスで複数選択
- 上部の検索欄で、表示名 / Model ID による絞り込みができます
- 一覧には **表示名** と **OpenRouter model ID** の両方を表示します

選択内容は `config/settings.json` に保存され、次回起動時に復元されます。

```json
{
  "extraction_model": "openai/gpt-4o",
  "generation_models": [
    "openai/gpt-4o",
    "anthropic/claude-sonnet-4",
    "google/gemini-2.5-pro"
  ]
}
```

保存済みのモデルが OpenRouter 上に見つからない場合は、メイン画面と設定ダイアログに
「⚠ OpenRouter 上に見つからないモデル: ...」と赤字で表示します。

## 7. 実験の実行

1. **Requirements** 欄に要求仕様を入力
2. **実行** を押す

以降は自動で次の順に進みます。ボタンは 1 つだけです。

```
Requirements
  → 抽出モデルで Decision Items を抽出 (実験につき 1 回だけ)
  → 同じ Requirements + 同じ Decision Items を各生成モデルへ
  → 各モデルの Decision Table を生成 (最大 4 並列)
  → 画面へ表示
  → outputs/ へ保存
```

- 処理状況は画面下部の「処理状況」とステータス欄に逐次表示されます
  (例: `Decision Items 抽出中...` → `GPT-4o で生成中...` → `生成完了: ...` → `完了`)。
- 結果はタブで表示されます。先頭が **Extracted Decision Items**、続いてモデルごとのタブ、
  最後に全モデルを横断比較する **「比較」** タブです (詳細は「12. 結果の比較」)。
  各タブで「プレビュー / Markdown ソース」を切り替えられ、「コピー」でクリップボードへコピーできます。
- 生成に失敗したモデルのタブは `⚠` 付きで表示され、`API Error: ...` を確認できます。
  1 つ失敗しても、他のモデルの結果は表示・保存されます。
- Decision Items の抽出自体に失敗した場合は、生成へ進まずエラーダイアログを表示します。

## 8. プロンプトの変更

プロンプトは Python コードではなく `prompts/` のテキストファイルで管理しています。
アプリは実行のたびに読み直すので、**再起動なしで差し替えられます**。

| ファイル | 用途 | 使えるプレースホルダ |
| --- | --- | --- |
| `prompts/extract.txt` | Decision Items 抽出 | `{requirements}` |
| `prompts/generate.txt` | Decision Table 生成 | `{requirements}` `{decision_items}` |
| `prompts/additional_description.txt` | 追加記述案の生成 | `{document}` `{difference}` |

プレースホルダを削除するとエラーになります (要求仕様が送られない事故を防ぐため)。

## 9. 実験結果の保存場所

1 回実行するごとに `outputs/<実行日時>/` が作られ、自動保存されます
(dataフォルダ一括実行の場合は `outputs/<仕様ファイル名>_<実行日時>/`。詳細は次節)。

```
outputs/
  20260916_140530/
    input.txt                                    入力した Requirements
    decision_items__anthropic_claude-haiku-4.5.txt  抽出された Decision Items (ファイル名で抽出モデルがわかる)
    openai_gpt-4o.md                              各生成モデルの Decision Table (失敗時はエラー内容)
    anthropic_claude-sonnet-4.md
    google_gemini-2.5-pro.md
    results.json                                  実行日時・使用モデル・所要時間・トークン数・全結果をまとめたもの
```

ファイル名はモデル ID から生成します (`/` などは `_` に置換)。
`results.json` には抽出・各生成モデルそれぞれの `elapsed_sec` (所要時間) と
`prompt_tokens` / `completion_tokens` / `total_tokens` (トークン数) が記録され、
どのファイルがどのモデルの出力かも `output_file` で対応付けられます。
データベースは使いません。メイン画面の「出力フォルダを開く」で保存先を開けます。

## 10. dataフォルダの一括実行

複数の要求仕様をまとめて実行したい場合は、`data/` フォルダに仕様を
`.txt` ファイルとして置き、メイン画面の **「一括実行 (dataフォルダ)...」** を押します。

- ダイアログに `data/` 内の `.txt` ファイルが一覧表示されるので、実行したいものだけ
  チェックして「実行」を押します (「すべて選択」「選択解除」も使えます)。
- 使用するモデルはメイン画面の「モデル設定...」で選択済みのものがそのまま使われます。
- 1ファイルずつ順番に (抽出 → 各生成モデルで並列生成 → 保存) を行い、
  1ファイルの抽出・生成に失敗しても処理は中断せず次のファイルへ進みます。
- 保存先は `outputs/<仕様ファイル名>_<実行日時>/` です
  (ファイル名部分の構成は上記「実験結果の保存場所」と同じ)。

### Excel (.xlsx) からの一括実行

`data/` に `.xlsx` を置くと、**1行 = 1件** として一覧に表示されます。

- ヘッダーに `番号` `区分` `テーマ` `内容` `解釈揺れ` `実行` の列があるシートだけを読み込みます
  (列の順番は自由。列がそろっていないシートは読み飛ばします)。
- 実行対象は「区分」が `変数追加版` で始まり、かつ「実行」列に `〇` が入っている行です。
- モデルに渡すのは「内容」列だけです。番号・区分・テーマ・解釈揺れ・揺れの内容・シート名・行番号は
  `results.json` の `source_meta` に記録します。
- 保存先は `outputs/<番号>_<テーマ>_変数追加版_<解釈揺れ>_<実行日時>/` です
  (例: `01_テーマパーク_変数追加版_あり_20261029_153000/`)。実行日時が付くので、再実行しても前の結果は残ります。
- 読み込み処理は `utils/excel_specs.py` にあります。列名や対象の区分を変える場合はファイル先頭の定数を変更してください。

## 11. 過去の実行結果を見る

メイン画面の **「過去の結果を見る...」** を押すと、`outputs/` 以下に保存済みの
実行結果 (手動実行・一括実行どちらも) を一覧から選んで、実行直後と同じ
タブ形式 (Extracted Decision Items + モデルごと + 比較) で再表示できます。
各タブには所要時間とトークン数も表示されるため、モデル間の比較・分析がしやすくなっています。

## 12. 結果の比較 (比較タブ)

実行直後の画面・過去の結果画面のどちらにも、全モデルの Decision Table を横断比較する
**「比較」** タブがあります。やっていることは次の通りです。

1. 各モデルの Markdown 表 (`区分 | 記述 | R1 | R2 ...`) を条件部・動作部の行に分解する。
2. **条件・動作の「記述」を正規化して対応付ける**
   (空白・改行の差異は無視するので、`会員である` と `会員  である` は同じ項目として扱われる)。
3. 全モデルに共通する条件の **組み合わせ単位** でルールをグループ化し、
   同じ組み合わせなのに **動作・結果の値がモデル間で食い違っているセルを赤くハイライト**する。
   あるモデルがその組み合わせのルールを生成していない場合はグレー表示になる。

### 表記ゆれがあって自動対応付けできない場合

モデルによって条件・動作の表記が微妙に違う (例: 「会員である」 vs 「会員ユーザーである」) ため
正規化だけでは対応付けられない項目は、比較タブ上部の **「表記ゆれの解決」** パネルに
一覧表示されます。そこから該当する項目を複数選択して
**「選択した項目を同じ項目として統合する」** を押すと、表示名を1つ入力するだけで
以後は同じ項目として扱われ、2. 以降と同じロジックで比較できるようになります。
統合は「統合をすべて解除」でいつでも取り消せます。

### その他

- 「結果が異なる組み合わせのみ表示」にチェックすると、一致している組み合わせを隠して
  差分のある組み合わせだけに絞り込めます。
- 比較対象は生成に成功し、かつ Markdown 表として解析できたモデルのみです。
  生成失敗・解析不能なモデルは比較対象外としてタブ上部に明示されます。
- 比較できるモデルが2つ未満の場合は、その旨のメッセージが表示されます。

### 追加記述案の作成 (解釈の分かれを解消する記述を LLM に提案させる)

比較タブ右上の **「追加記述案を作成...」** から、解釈が分かれている箇所について
「それぞれの解釈に一意に定まるような追加記述」を LLM に提案させられます。

1. **差異の指定** (ダイアログ上部のプルダウン)
   - `比較表で選択: 組合せN` — 比較表でセル (列) を選択してからボタンを押すと、その組み合わせが選ばれた状態で開く
   - `自動検出: 組合せN` — モデル間で結果が食い違った組み合わせ (赤いセル) が自動で並ぶ
   - `手動: <モデル> の Rn をひな形にする` — 任意のルール列の条件・結果を読み込み、もう1つの解釈を自分で書き足す
     (モデルが1つだけの実行でも使える)
   - `手動: 自由入力` — 条件・解釈をすべて自分で書く
2. 条件 (1行1つ) と分かれた解釈 (結果動作) は編集でき、編集すると次の文章が自動で組み立てられます。
   文章そのものを直接書き換えてもかまいません。
   ```
   条件が「来場人数が1人である（成立）、来場した団体が全員学生である（成立）」の時に、結果動作が「入場料金：4750」と「入場料金：4250」になる。
   ```
3. モデルを選んで **「追加記述案を生成」** を押すと、元の文書 (Requirements) とこの文章が LLM に渡され、
   `「入場料金：4750」に対応した記述：…` の形式で追加記述案が返ってきます。
4. 結果はその実行結果フォルダの `additional_descriptions/` に自動保存されます。
   - `<生成日時>__<モデルID>.md` — 差異の文章・各解釈をとったモデル/ルール・追加記述案
   - `<生成日時>__<モデルID>.json` — 上記 + 元の文書・トークン数・所要時間

プロンプトは `prompts/additional_description.txt` (プレースホルダ: `{document}` `{difference}`) で、
他のプロンプトと同じく再起動なしで差し替えられます。処理本体は `services/additional_description_service.py`、
画面は `ui/additional_description_dialog.py` です。

### 実装の場所 (拡張する場合)

| ファイル | 役割 |
| --- | --- |
| `models/decision_table.py` | パース結果 (`DecisionTable` / `DecisionRow`) と入力 (`TableSource`) のデータ構造 |
| `services/decision_table_parser.py` | モデル出力の Markdown 表を `DecisionTable` へ変換 (Qt非依存) |
| `services/table_comparison.py` | ラベルの正規化・対応付け・組み合わせ単位の比較ロジック本体 (Qt非依存、純粋関数ベース) |
| `ui/comparison_view.py` | 比較タブの描画とユーザー操作 (表記ゆれの手動統合など) |

比較の計算ロジックはすべて `services/table_comparison.py` にあり、Qt に依存しないため
単体で動作確認・拡張がしやすい構成になっています。

---

## フォルダ構成

```
tool/
├─ main.py                     エントリポイント
├─ requirements.txt
├─ .env / .env.example         OpenRouter APIキー
│
├─ config/
│   ├─ settings.json           選択した抽出モデル・生成モデル
│   └─ models_cache.json       モデル一覧のキャッシュ (自動生成)
│
├─ prompts/
│   ├─ extract.txt             Decision Items 抽出プロンプト
│   ├─ generate.txt            Decision Table 生成プロンプト
│   └─ additional_description.txt  追加記述案の生成プロンプト
│
├─ ui/
│   ├─ main_window.py          メイン画面 (入力・実行・状況・結果)
│   ├─ settings_dialog.py      モデル設定ダイアログ
│   ├─ batch_dialog.py         dataフォルダ一括実行ダイアログ
│   ├─ history_dialog.py       過去の実行結果の一覧・再表示ダイアログ
│   ├─ result_view.py          Markdown 表示 + コピー用の共通ウィジェット
│   ├─ comparison_view.py      「比較」タブ (全モデルの Decision Table を横断比較)
│   └─ additional_description_dialog.py  追加記述案の作成ダイアログ
│
├─ services/
│   ├─ openrouter_client.py    OpenRouter との通信 (ここだけが HTTP を触る、トークン数も返す)
│   ├─ model_service.py        モデル一覧の取得・キャッシュ・検証
│   ├─ extraction_service.py   Decision Items 抽出
│   ├─ generation_service.py   Decision Table 生成
│   ├─ step_runner.py          抽出/生成 1回分の実行ヘルパー (時間計測・例外処理を共通化)
│   ├─ experiment_runner.py    実験1回の流れ (抽出→生成→保存) をバックグラウンド実行
│   ├─ batch_runner.py         dataフォルダの複数仕様をまとめて実行するバックグラウンド処理
│   ├─ decision_table_parser.py  Markdown の Decision Table を構造化データへ変換
│   ├─ table_comparison.py     複数 Decision Table の正規化・対応付け・比較ロジック (Qt非依存)
│   └─ additional_description_service.py  差異の文章化・追加記述案の LLM 呼び出しと保存 (Qt非依存)
│
├─ models/
│   ├─ experiment.py           Experiment / GenerationResult
│   └─ decision_table.py       DecisionTable / DecisionRow / TableSource
│
├─ utils/
│   ├─ config_manager.py       .env と settings.json、パス定義
│   └─ file_manager.py         プロンプト読み込み、outputs への保存、過去実験の一覧・読み込み
│
├─ data/                       一括実行の入力となる仕様テキストファイル (*.txt) を置く場所
│
└─ outputs/                    実験結果 (実行ごとにフォルダ)
```

処理の流れを追うときは `services/experiment_runner.py` と `services/batch_runner.py` の
`run()` を読むのが早道です (どちらも `services/step_runner.py` を介して抽出・生成を行います)。
比較機能の計算ロジックを追うときは `services/table_comparison.py` の
`align_labels()` → `compare_tables()` の流れを読むのが早道です。

## 今後の拡張について

- 複数実行 (別日時の `outputs/` フォルダ) をまたいだ比較は未対応です。現状は
  1回の実行内 (同じ Requirements + 同じ Decision Items から生成した複数モデル) の比較のみです。
- 不完全性判定 (条件の組み合わせの網羅漏れなど) は未実装です。
- 複数実行結果の読み込みは `utils/file_manager.py` の `list_output_runs()` / `load_experiment_record()`
  が既にあるので、それを使って `results.json` を読み込めます。
