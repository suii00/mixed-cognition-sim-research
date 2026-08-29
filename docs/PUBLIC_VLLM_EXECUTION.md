# Public vLLM execution contract v1.0.0

## Scope

`tools/run_public_vllm.py` は発展経路である vLLM 実験の標準入口です。既定 config は
`configs/public_vllm_smoke_3model.json` で、実成果物と同じ Qwen 2.5 7B、Llama 3.1 8B、
Gemma 2 9B、4096 context、`phase-response-v2.0.0` を使います。これは engineering smoke
であり、研究結論の証拠ではありません。

Ollama は `main.py` を入口とする基礎経路です。provider、sampling、response contract、
runtimeが異なるため、Ollama runをvLLM成果物の再現としては扱いません。

## Exact runtime

次の三ファイルを一組として更新します。

- `.python-version`
- `requirements-vllm.lock.txt`
- `runtime/vllm-runtime-lock.json`

launcher は Python implementation、patch version、主要 package version の完全一致を
起動前に検査します。範囲指定や「互換と思われる」version では実行しません。

検証済み profile は、歴史的vLLM成果物で記録されたCPython 3.12.14、vLLM 0.27.1、
torch 2.13.0+cu132、`flashinfer-python` 0.6.16.post3を完全一致で固定します。
temporary import boundaryでFlashInferをunavailableとし、FlashInfer samplerと
all-reduceを明示的に無効化します。temporary boundaryとcompile cacheはrunごとに
一時生成して終了時に削除し、repositoryやrunには入りません。

Gemma 2 9BをTP1、context 4096、fresh compile cacheで起動するconfigは
`gpu_memory_utilization=0.95`を使います。`0.92`で成功した歴史的dual-worker endpointは
永続AOT cacheを再利用しており、fresh-cache profileでは同じmemory余裕を再現しません。
この調整はmodel、context、prompt、sampling、response contractを変更しません。

## No-log server lifecycle

各 logical endpoint に対して launcher は次を行います。

1. config の model source と commit digest から exact local snapshot を解決する。
2. snapshot directory を process working directory とし、vLLM には model `.` だけを渡す。
3. child environment を allowlist から構成し、offline と telemetry-disabled を強制する。
4. loopback だけに bindし、request logging を無効にする。
5. stdin、stdout、stderr を直接 null device へ接続する。pipe、server log、startup log file は
   作らない。
6. `/health` と `/v1/models` を proxy 無効の local request で検査する。
7. simulation の成否にかかわらず process group へ interrupt、terminate、kill の順で停止を
   試み、全 process の終了を確認する。

server の raw diagnostic を後から消す設計ではありません。raw diagnostic を最初から
永続化しない設計です。startup failure は公開可能な固定 category だけを返します。

## GPU boundary

GPU は `CUDA_VISIBLE_DEVICES` で endpoint ごとに割り当てます。既定 smoke は4 GPU、hard
limit は6 GPUです。launcher は次を fail closed で確認します。

- index の存在、重複なし、tensor-parallel demand との完全一致
- 起動前の selected GPU が設定値より空いていること
- 全 GPU の memory deltaを監視し、selected scope 外の新規使用を検出しないこと
- 同時に active になった GPU が6以下であること
- cleanup 後に selected GPU が baseline 近傍へ戻ること

monitor は device-unique ID、network address、process command line を取得しません。別利用者が
同時に GPU を起動した場合も scope escape と区別できないため、安全側に停止する制約が
あります。

## Run promotion and proof

simulation は `runs/.tmp/` の ignored staging directoryへ出力します。これは public config と
temporary loopback bindingを engine に渡すためであり、raw bytes の変換ではありません。

完了後に次を実行します。

- strict run validation
- expected step/agent coverage と terminal status の照合
- runtime binding の byte-level 非残存確認
- run tree の publication scan
- process group 停止と GPU release の確認

publication finding が1件でもある run は public treeへ移しません。finding 0 の run は bytesを
変更せず `runs/output_<run_id>/` へ atomic moveします。安全な aborted/negative run は変換せず
保存できます。検証結果は timestampを含む run IDごとの
`derived/validation-vllm-<run_id>/verification.json` に保存します。

## Commands

静的 contract 検査:

```bash
python tools/run_public_vllm.py --contract-only
```

GPU、package、model snapshot、port を含む起動前検査:

```bash
python tools/run_public_vllm.py --preflight-only
```

既定 smoke:

```bash
python tools/run_public_vllm.py
```

新しい vLLM 実験:

```bash
python tools/run_public_vllm.py --config configs/<public-vllm-config>
```

config は logical endpoint、exact model digest、context、sampling、response contract を保持し、
runtime address や実機固有 ID を保持しません。

## Formal 60-run matrix

`tools/run_public_disaster_matrix.py`は同じpublic-by-construction primitiveを使い、
`configs/public_formal_disaster_v3_2/manifest.json`に固定した60 runを二つのworker laneで実行します。
serverはQwen replica 2、Llama replica 2、Gemma TP2 shared 1の計5 process、6 GPUです。

```bash
python tools/build_public_disaster_matrix.py --check
python tools/run_public_disaster_matrix.py \
  --source-git-sha <full-sha> \
  --contract-only
python tools/run_public_disaster_matrix.py \
  --source-git-sha <full-sha> \
  --preflight-only
```

本実行はcleanな承認済みsource SHA、GPU indices、8時間以下のwall limitを要求します。
各workerは30 run/72,000 call、全体は60 run/144,000 callです。retryは許可せず、HTTP attemptも
144,000をhard ceilingとします。一つでもrun、server、GPU scope、strict validation、scan、cleanupが
失敗すると全体を停止し、どのrunもpublic treeへ昇格しません。成功時のaggregate evidenceは
`derived/validation-vllm-matrix-<batch_id>/verification.json`です。

strict validationの`unverifiable`はerrorとは別のepistemic limitationです。launcherは
`valid == true`を必須とし、unverifiableを隠さず件数とdigestでevidenceへ記録します。

条件、seed、介入、対照、観測連鎖は
`docs/EXPERIMENT_PROTOCOL_PUBLIC_DISASTER_V3_2.md`に事前登録されています。
