# Local execution

Ollama はローカル最小実行の基礎経路、vLLM は固定 runtime と複数 GPU を使う
発展経路です。backend は実験条件なので、両者の run を同一条件として混合しません。

## Foundational Ollama path

`configs/smoke_local.yaml` は公開条件、
`configs/runtime-bindings.loopback.example.yaml` は loopback routing example です。
Ollama serviceを起動し、configが使うモデルを用意してから実行します。virtual
environmentを使う場合は、先にそのshellの方法でactivateしてください。

```bash
python -m pip install -r requirements.txt
ollama pull qwen2.5:3b
ollama pull gemma3:4b
ollama pull llama3.2:3b
ollama list
python main.py --config configs/smoke_local.yaml --runtime-bindings configs/runtime-bindings.loopback.example.yaml --output-root runs
```

Remote binding は repository 外に置くか、ignore 対象の local binding file を使います。
URL 内 credential と authentication header はこの simulator の公開実行 contract 外です。

## Advanced vLLM path

公開安全な vLLM の起動、GPU 割当、health check、simulation、strict validation、cleanup は
一つの command で実行します。現在収録している正式vLLM成果物を再現する場合は、
このlauncherが標準入口です。

```bash
python tools/run_public_vllm.py --preflight-only
python tools/run_public_vllm.py
```

別の公開 config も同じ境界を使います。

```bash
python tools/run_public_vllm.py --config configs/<public-vllm-config>
```

この launcher は runtime binding を一時生成し、server output を保存せず、検証済み run
bytes だけを `runs/` へ移します。手作業の runtime-binding 作成はvLLM経路では不要です。

## Completion check

```bash
python tools/validate_run.py runs/output_<run_id> --strict
```

成功条件は process exit code だけではありません。terminal meta が `completed`、`aborted`
が false、expected step/agent coverage が存在し、raw manifest と schema validation が通る
ことを確認します。
