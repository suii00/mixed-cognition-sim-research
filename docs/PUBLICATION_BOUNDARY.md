# Publication boundary v1.0.0

## Design goal

公開用の別 snapshot を作るのではなく、研究 repository 自体を公開可能な状態に保つ。
raw run と config は一つだけ存在し、公開前に内容を変換する工程を置かない。

## Two input planes

Public experiment config:

```yaml
blocs:
  - name: alpha
    model: example/model
    endpoint_id: worker-a
    device_slot: accelerator-0
    num_agents: 4
```

Runtime binding:

```yaml
endpoints:
  worker-a:
    base_url: "http://127.0.0.1:8000"
```

前者だけが config hash と provenance の対象です。後者は process memory 内で endpoint
解決に使われますが、RunLifecycle へ渡されず、run artifact に保存されません。
runtime binding は研究条件ではなく運用上の routing です。

## Values excluded by construction

Public config と run metadata は次を受け付けません。

- network endpoint address
- host name
- GPU/device globally unique identifier
- process-local accelerator visibility string
- authentication header、token、password、private key
- credential を含む URI

GPU probe は `index,name,memory.total,driver_version` のみを問い合わせます。endpoint と
accelerator の割当を再現するための公開 identity は `endpoint_id` と `device_slot` です。
これらは実機固有の識別子であってはなりません。

## No transformation rule

Repository に redaction/sanitization output は存在しません。

- config snapshot は effective public config の完全な deep copy です。
- config hash は同じ object の canonical JSON SHA-256 です。
- raw JSONL は生成後に編集しません。
- remote run の ingest は source と destination の全 file SHA-256 を比較します。
- validation と scanning は read-only で、失敗時に停止します。

問題のある run は変換して公開可能にしません。その run を immutable な失敗証拠として
非公開環境に保持し、原因となる入力境界を修正した上で、新しい run ID で再実行します。

## Repository verification

```bash
python tools/verify_repository.py
```

この command は全 public config を load/validate し、`runs/output_*` を strict validation
し、tree 全体を publication scanner で検査します。Git history を含む追加確認は次です。

```bash
python tools/scan_publication.py . --git-history
```

どちらの command も file を変更しません。
