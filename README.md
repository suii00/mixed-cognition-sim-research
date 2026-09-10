# mixed-cognition-sim

異なるLLMを同じ初期世界・災害条件で比較するマルチエージェントシミュレーション。
守りたいのは、災害時にも情報を共有し、人や社会が状況を確かめて行動を調整できる機能です。
本研究でいう「メタ安全保障」は、その前提となるAI間の情報伝達と行動の連鎖を検証可能にするという研究目的です。現実の安全性向上を実証したという意味ではありません。

**2026-09-10 警報保持方式の対応比較:** [6 runの観測結果とA/B比較HTML](docs/RESULTS_WARNING_RETENTION_STUDY_20260910.md)。混成24体・60 steps・3組のseedを完走しました（17,280 calls、失敗・再試行0）。公式警報を保持するB条件では、直接受信者への提示が全seed・両Phaseで100%になりました。一方、警報IDを含む発話の生成・配送・後続再使用は全6 runで0でした。入力の継続提示だけでは、この条件のexact-ID伝達は増えませんでした。3組の探索的観測であり、言い換えを含む共有全般や理解の有無は判定していません。[事前protocol](docs/EXPERIMENT_PROTOCOL_WARNING_RETENTION_STUDY_V1.md)、生ログ、指標仕様、独立照合済みの解析と検証記録を収録しています。

**2026-09-10 混成24体の追加実験:** [避難所配置・60/120 stepの観測結果と4本のreplay](docs/RESULTS_REFUGE_LAYOUT_STUDY_20260910.md)。Qwen・Llama・Gemma各8体が同居する全4 runが完走しました（17,280 calls、失敗・再試行0）。[事前protocol](docs/EXPERIMENT_PROTOCOL_REFUGE_LAYOUT_STUDY_V1.md)と[計測仕様](docs/REFUGE_LAYOUT_STUDY_METRIC_V1_SPEC.md)を固定し、初期位置・モデル割当・初期警報受信者をそろえました。独立60-step runの期間内到達は端0/24体、中央寄り1/24体。120-step run自身の60→120 stepでは、端0→0体、中央寄り2→3体でした。終点での避難所内在所、警報IDの後続再使用は全条件で0です。原本JSONL・全条件の集計・図・検証記録を収録しています。1 seedの記述的観測であり、24体を独立な反復数として扱いません。

エージェントをブロック単位で異なるLLMに割り当て、ブロック／モデル情報をエージェント向けプロンプトに含めません。実験ごとにworld・prompt・sampling・通信条件を定め、model artifact、tokenizer、chat template、推論実装を記録して出力と行動を比較します。望ましい結論へ誘導する役割や報酬は与えず、条件と生ログから観測をたどれるようにします。

**2026-09-09 追加実験:** [災害下のLLM行動比較・6 runの観測結果](docs/RESULTS_DISASTER_BEHAVIOR_PILOT_20260909.md)。
[事前登録](docs/EXPERIMENT_PROTOCOL_DISASTER_BEHAVIOR_PILOT_V1.md)したQwen・Llama・Gemma × 2 seed、各4 agents・60 stepsの全6 runが完走しました（2,880 calls、失敗・再試行0）。
事前規則の例ではQwenが左、Llama/Gemmaが右を選択しました。警報IDの後続再使用は露出した6 agent-run中3件、避難所到達は全24 agent-runで0です。生ログ・集計・全軌跡図・検証記録を収録し、初期世界の一致と分岐後の入力差、出力上の再使用と行動への採用を区別して報告しています。少数runの記述的観測であり、モデルの優劣や実災害での有効性は示しません。

https://github.com/user-attachments/assets/e4de5be5-a67b-48d8-8d20-2f84854d4cf9

## クレジットと系譜

数値情報のみを与え定性評価を排除する観察パラダイムは、シンギュラボ ハッカソン
Vol.1 課題（GPL-3.0）に由来し、AUTOMATA ハッカソン Vol.2 では公式デモ
[ryukih/SD-Hackathon-2026DEMO](https://github.com/ryukih/SD-Hackathon-2026DEMO)
（Apache-2.0, © 2026 Dr. Ryuki HYODO / SpaceData Inc.）として提供されている。

本リポジトリは設計仕様書からのスクラッチ実装であり、上記からのコード流用は
ない。4フェーズ実行順序・通信制約・jsonl フィールド名は、Vol.1 で筆者が構築した
[suii00/2d-multi-places-simulation-on-fire-public](https://github.com/suii00/2d-multi-places-simulation-on-fire-public)
を適用可能にするため、意図的に互換を保っている。

## 実行経路の位置づけ

Ollama は、本シミュレータの原設計とローカル最小実行を支える基礎経路です。
エージェント別モデルルーティング、4フェーズ同期、通信境界、ログ生成を小規模な構成で
確認する場合は、まず Ollama を使います。

vLLM は、commit digestで固定したlocal model snapshot、複数 GPU、高スループット、
厳密な起動・検証・cleanupを加えた発展経路です。現在収録している正式成果物と
公開用60-run matrixの再現にはvLLMを使います。

| | Ollama | vLLM |
|---|---|---|
| 位置づけ | 基礎経路 | 発展経路 |
| 主用途 | ローカル smoke、ルーティング・simulation・log の確認 | 複数 GPU 実行、正式成果物・matrix の再現 |
| 入口 | `main.py` | `tools/run_public_vllm.py` |
| 主な前提 | 稼働中の Ollama と取得済みモデル | 固定 runtime、local snapshot（既定 smoke は4 GPU、matrix は最大6 GPU） |

backend は実験条件の一部です。Ollama run と vLLM run は自動的に同一条件とはみなさず、
provider、model artifact、sampling、response contract、runtime versionを記録して区別します。

## 基礎実行経路: Ollama

Ollama serviceを起動し、`configs/smoke_local.yaml` が使う三つのモデルを用意します。
既に取得済みの場合も、`ollama list` でモデル名を確認してください。
次はWindows PowerShellでの例です。他の環境では、作成したvirtual environmentを
そのshellの方法でactivateしてから同じPython commandを実行してください。

```powershell
python -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
ollama pull qwen2.5:3b
ollama pull gemma3:4b
ollama pull llama3.2:3b
ollama list
.\.venv\Scripts\python.exe main.py --config configs/smoke_local.yaml --runtime-bindings configs/runtime-bindings.loopback.example.yaml --output-root runs
```

成功すると `runs/output_<run_id>/` が新規作成されます。同じ run ID の再実行は
衝突として拒否され、既存 raw log へ追記されません。exit codeだけで成功とせず、
生成されたrunをstrict validationします。

```powershell
.\.venv\Scripts\python.exe tools/validate_run.py runs/output_<run_id> --strict
```

実サービス用 binding は、example を元に repository 外または ignore 対象の
`runtime-bindings.local.yaml` として作成してください。credential を URL に埋め込む
形式は未対応です。詳しくは
[Local execution](docs/SIMPLE_BACKEND_EXECUTION.md) を参照してください。

## 発展実行経路: vLLM

vLLMは、現在収録している正式成果物を再現する場合の標準入口です。検証済み runtime は
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
3. backendを事前登録した実験条件として選びます。ローカルの基礎確認は
   `python main.py --config <config> --runtime-bindings <binding> --output-root runs`、
   単一vLLM実験は `python tools/run_public_vllm.py --config <config>`、固定した正式matrixは
   専用の`run_public_disaster_matrix.py`で実行します。backendを変更したrunを同一条件として
   混合せず、必要なら新しいprotocol/configとして事前登録します。remote machine で別出力へ
   生成した場合だけ `python tools/ingest_run.py <output_dir>` で同一 bytes を取り込みます。
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
