"""Publisher fixtures use a real persisted presentation receipt and snapshot."""
import json
import uuid
from AstralNexus.core.delivery_snapshot import authorize_snapshot,snapshot,FRAMEWORK_FILES
from AstralNexus.runtime.execution_store import ExecutionStore
from AstralNexus.runtime.presentation import delivery_receipt


def authorize_files(manager, *, ownership_identity=None):
    rows=snapshot(manager.staging_dir)
    files={"outputs/"+name:(sha,size) for name,sha,size in rows if name not in FRAMEWORK_FILES}
    receipt=delivery_receipt({},files,list(files))
    run_id="fixture:"+uuid.uuid4().hex
    receipt.update(run_id=run_id)
    receipt=ExecutionStore(manager.run_dir).record_delivery(run_id,receipt)
    recorded=manager.staging_dir/"delivery_verification.json"
    gate=json.loads(recorded.read_text(encoding="utf-8")) if recorded.is_file() else {"overall_status":"pass"}
    gate.update(gate_policy_version="presented-output-v1",delivery_receipt=receipt,receipt_store_root=str(manager.run_dir.resolve()))
    if recorded.is_file():
        recorded.write_text(json.dumps(gate,ensure_ascii=False),encoding="utf-8")
        summary=manager.staging_dir/"delivery_summary.json"
        if summary.is_file():
            value=json.loads(summary.read_text(encoding="utf-8"));value["delivery_gate"]=gate
            summary.write_text(json.dumps(value,ensure_ascii=False),encoding="utf-8")
    return authorize_snapshot(manager.staging_dir,{},gate,ownership_identity=ownership_identity)
