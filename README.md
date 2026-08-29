# Mixed-Cognition Social Simulation

異なる LLM が混在する社会シミュレーションを、再現可能かつ監査可能な
形で実行・検証・公開するための独立リポジトリです。このリポジトリだけで
simulation、disaster scenario、parallel transport、metrics、可視化、run 検証を
実行でき、別リポジトリのコードやデータを参照しません。

この設計にはサニタイズ工程がありません。公開設定と実行時接続情報を入力時点で
分離し、run 成果物は最初から公開可能な値だけで生成します。検証コマンドは失敗を
報告するだけで、config や raw log を書き換えません。

## 公開境界

- `configs/` は公開可能な研究条件だけを保持します。endpoint は論理的な
  `endpoint_id` と任意の `device_slot` で表します。
- 実 URL は別の runtime-binding ファイルからメモリ上へ読み込みます。binding の
  値は config snapshot、`run_meta.json`、raw JSONL へコピーされません。
- public config 内の接続 URL、host 名、GPU 固有 ID、credential-shaped key は実行前に
 拒否されます。
- GPU provenance は index、製品名、memory、driver/CUDA version までに限定し、
  device 固有 ID を取得しません。
- `runs/` は意図的に Git 管理対象です。新しい run を追加するために snapshot
  generator や sanitizer を通す必要はありません。

詳細は [Publication boundary](docs/PUBLICATION_BOUNDARY.md) を参照してください。

## クレジットと系譜

数値情報のみを与え定性評価を排除する観察パラダイムは、シンギュラボ ハッカソン
Vol.1 課題（GPL-3.0）に由来し、AUTOMATA ハッカソン Vol.2 では公式デモ
[ryukih/SD-Hackathon-2026DEMO](https://github.com/ryukih/SD-Hackathon-2026DEMO)
（Apache-2.0, © 2026 Dr. Ryuki HYODO / SpaceData Inc.）として提供されている。

本リポジトリは設計仕様書からのスクラッチ実装であり、上記からのコード流用は
ない。4フェーズ実行順序・通信制約・jsonl フィールド名は、Vol.1 で筆者が構築した
[suii00/2d-multi-places-simulation-on-fire-public](https://github.com/suii00/2d-multi-places-simulation-on-fire-public)
を適用可能にするため、意図的に互換を保っている。

## 標準実行経路: vLLM

実成果物の主要 backend に合わせ、vLLM を標準実行経路とします。検証済み runtime は
CPython 3.12.14、vLLM 0.27.1、torch 2.13.0+cu132 です。FlashInfer 0.6.16.post3 は
環境同一性のため記録・固定し、launcher は sampler と all-reduce を import 前に無効化して
torch/FlashAttention 経路を使います。compile cache はrunごとに一時生成し、公開成果物へ
保存しません。

```bash
python -m venv .venv
python -m pip install -r requirements.txt -r requirements-vllm.lock.txt
python tools/run_public_vllm.py --contract-only
python tools/run_public_vllm.py --preflight-only
python tools/run_public_vllm.py
```

既定の smoke は、実成果物と同じ三モデル、4096 context、
`phase-response-v2.0.0` response contract を使います。Qwen と Llama に各1 GPU、
Gemma に2 GPUを割り当て、合計4 GPUで動作します。launcher の上限は6 GPUで、これを
引き上げる CLI option はありません。

モデルは config に記録された commit digest の local snapshot だけを offline で使います。
launcher は download や authentication を行わず、認証用環境変数も子 process へ渡しません。
必要な snapshot は実行前に用意してください。

成功すると `runs/output_<run_id>/` と
`derived/validation-vllm-<run_id>/verification.json` が作られます。後者は strict validation、
publication finding 0、runtime-binding 非残存、process cleanup、GPU release の機械可読な
検証結果です。

詳細は [Public vLLM execution](docs/PUBLIC_VLLM_EXECUTION.md) を参照してください。

## 公開用60-run正式matrix

QQQ、LLL、GGG、三モデル混合を、三つのcommunication条件と五つの事前固定seedで
実行する正式matrixも、このリポジトリ単体で生成・起動・検証できます。全体は60 run、
144,000 logical callです。Qwen/Llamaを二つずつ、GemmaをTP2で一つ起動し、最大6 GPUを
使用します。

```bash
python tools/build_public_disaster_matrix.py --check
python tools/run_public_disaster_matrix.py --source-git-sha <full-sha> --contract-only
python tools/run_public_disaster_matrix.py --source-git-sha <full-sha> --preflight-only
python tools/run_public_disaster_matrix.py --source-git-sha <approved-full-sha> --gpu-indices 0,1,2,3,4,5
```

launcherは全60 runをignored stagingへ生成し、各runと全体を二重検証します。全件がstrict
PASS、HTTP retry/failure 0、publication finding 0、runtime-binding残存0の場合だけraw bytesを
`runs/`へ昇格します。途中失敗したmatrixを部分成果として公開したり、出力をsanitizationして
成功扱いにしたりしません。実験条件と観測連鎖は
[Public disaster formal protocol v3.2](docs/EXPERIMENT_PROTOCOL_PUBLIC_DISASTER_V3_2.md) に事前登録しています。

## 補助実行経路: Ollama

Ollama は小規模な補助確認用です。主要成果物の再現経路ではありません。

```bash
python main.py \
  --config configs/smoke_local.yaml \
  --runtime-bindings configs/runtime-bindings.loopback.example.yaml \
  --output-root runs
```

成功すると `runs/output_<run_id>/` が新規作成されます。同じ run ID の再実行は
衝突として拒否され、既存 raw log へ追記されません。

実サービス用 binding は、example を元に repository 外または ignore 対象の
`runtime-bindings.local.yaml` として作成してください。credential を URL に埋め込む
形式は未対応です。

## 歴史的runの独立再現

旧リポジトリの10条件は、旧prompt bytes、seed、モデルdigest、sampling、endpoint
poolを固定した公開configとして `configs/legacy_reproduction_v1/` に収録しています。
これは正式v2実験とは分離されたengineering reproductionであり、全configが
`research_eligible=false`です。

```bash
python tools/build_legacy_reproduction_matrix.py --check
python tools/run_legacy_reproduction.py --contract-only
python tools/run_legacy_reproduction.py \
  --provider vllm \
  --preflight-only \
  --source-git-sha <approved-full-sha> \
  --gpu-indices 0,1,2,3,4,5
python tools/run_legacy_reproduction.py \
  --provider vllm \
  --execute \
  --source-git-sha <approved-full-sha> \
  --gpu-indices 0,1,2,3,4,5
python tools/run_legacy_reproduction.py \
  --provider ollama \
  --preflight-only \
  --source-git-sha <approved-full-sha> \
  --gpu-indices 0,1,2 \
  --ollama-model-root <read-only-model-root>
```

launcherは最大6 GPUを強制します。この許可内では8/10条件を実行可能です。元の
7 GPU endpoint poolを使う2条件は黙って縮小されず、別の承認があるまで`not_run`
として残ります。同じsimulation seedはworld状態を再現しますが、旧runはLLM生成seedを
送っていないため、同一テキストやraw bytesは保証しません。詳細は
[Historical Run Reproduction Protocol](docs/EXPERIMENT_PROTOCOL_LEGACY_REPRODUCTION_V1.md)
を参照してください。Ollamaのmodel rootは実行時引数としてのみ渡され、公開config、
run、検証記録には保存されません。複数のvLLM serverは共有startup deadline内で
順次起動し、全endpointのhealth check後にのみsimulationを開始します。
Gemma 2 9BのTP1/4096 contextはfresh compile cacheでも起動できるよう
`gpu_memory_utilization=0.95`を固定します。これはmodel、context、prompt、sampling、
response contractを変えない運用上のmemory予約です。

## 新しい実験

1. `docs/EXPERIMENT_PROTOCOL.md` の項目を事前登録し、protocol version を決めます。
2. 既存 config を新しい名前へコピーし、`run_id`、seed、介入、対照、model 条件を
   明示します。public config に runtime 値は書きません。
3. 単一vLLM実験は `python tools/run_public_vllm.py --config <config>`、固定した正式matrixは
   専用の`run_public_disaster_matrix.py`で実行します。
   remote machine で別出力へ生成した場合だけ
   `python tools/ingest_run.py <output_dir>` で同一 bytes を取り込みます。
4. run 単体と repository 全体を検証します。

```bash
python tools/validate_run.py runs/output_<run_id> --strict
python tools/verify_repository.py
python -m pytest -q
```

`verify_repository.py` と `scan_publication.py` は読み取り専用です。検出値を削除・置換・
再構成せず、問題があれば非ゼロで停止します。

## 主要ディレクトリ

- `engine/`: simulator、phase barrier、LLM transport、provenance
- `configs/`: 公開実験 config と安全な runtime-binding example
- `runs/`: immutable な公開 raw run
- `derived/`: version 付き metric・report・visualization
- `tools/`: matrix builder、probe、validator、metric、可視化
- `tests/`: barrier、通信境界、run 衝突、abort、schema、公開境界の回帰テスト

## 証拠の読み方

受信は `exposure` であって `reuse` や `adoption` ではありません。`reuse` は受信後の
別 step で受信者自身が生成した出力により判定します。単一 run や単一引用を頑健性・
因果の証拠とは扱いません。詳細な規律は [AGENTS.md](AGENTS.md) と各 protocol/metric
spec にあります。

## License

[LICENSE.txt](LICENSE.txt) を参照してください。
