# vLLM transport contract v2.0.0

vLLM bloc は public config で `provider: vllm` を選択し、model source/digest、tokenizer
revision、backend version、dtype、quantization、chat template、generation config、model
length、tensor/data parallel size を明示する。接続 address は runtime binding にだけ置く。

Transport は OpenAI-compatible chat-completion endpoint を使用する。phase-response-v2.0.0
では repository-owned JSON schema を Phase 1 と Phase 3 ごとに渡し、返却 JSON を同じ
contract で再検証する。

Schema 2.0.0 run では各 logical request に対して `llm_attempts.jsonl` が response envelope、
raw output、HTTP body bytes の base64、byte count、SHA-256、usage、finish reason、parse/schema
status を記録する。address、host、device 固有 ID、credential は記録しない。

Transport、syntax parse、response schema の terminal failure は run を abort する。並列
worker の完了順は commit 順に影響せず、phase barrier を越えて partial result を適用しない。
