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

## 標準実行経路: vLLM

実成果物の主要 backend に合わせ、vLLM を標準実行経路とします。検証済み runtime は
CPython 3.10.12、vLLM 0.27.1、torch 2.13.0 です。FlashInfer 0.6.16.post3 は
環境同一性のため記録・固定しますが、この Python 組合せで生じる import 不整合を避ける
ため、launcher が import 前に無効化し、torch 経路を使います。

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

## 新しい実験

1. `docs/EXPERIMENT_PROTOCOL.md` の項目を事前登録し、protocol version を決めます。
2. 既存 config を新しい名前へコピーし、`run_id`、seed、介入、対照、model 条件を
   明示します。public config に runtime 値は書きません。
3. vLLM 実験は `python tools/run_public_vllm.py --config <config>` で実行します。
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
