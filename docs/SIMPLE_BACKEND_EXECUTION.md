# Local execution

## Standard vLLM path

公開安全な vLLM の起動、GPU 割当、health check、simulation、strict validation、cleanup は
一つの command で実行します。

```bash
python tools/run_public_vllm.py --preflight-only
python tools/run_public_vllm.py
```

別の公開 config も同じ境界を使います。

```bash
python tools/run_public_vllm.py --config configs/<public-vllm-config>
```

この launcher は runtime binding を一時生成し、server output を保存せず、検証済み run
bytes だけを `runs/` へ移します。手作業の runtime-binding 作成は標準経路では不要です。

## Auxiliary Ollama path

`configs/smoke_local.yaml` は公開条件、
`configs/runtime-bindings.loopback.example.yaml` は loopback routing example です。

```bash
python main.py --config configs/smoke_local.yaml \
  --runtime-bindings configs/runtime-bindings.loopback.example.yaml \
  --output-root runs
```

Remote binding は repository 外に置くか、ignore 対象の local binding file を使います。
URL 内 credential と authentication header はこの simulator の公開実行 contract 外です。

## Completion check

```bash
python tools/validate_run.py runs/output_<run_id> --strict
```

成功条件は process exit code だけではありません。terminal meta が `completed`、`aborted`
が false、expected step/agent coverage が存在し、raw manifest と schema validation が通る
ことを確認します。
