"""Run with python -m invoicedesk.interfaces.worker. One process per concurrent parser."""
import os
import threading
import signal
import time
from pathlib import Path
from invoicedesk.application.jobs import JobService
from invoicedesk.domain.approval import Conflict
from invoicedesk.infrastructure.extraction.llm import LlmClient
from invoicedesk.infrastructure.preflight import require_production_dependencies
from invoicedesk.infrastructure.repository import Repository
from invoicedesk.infrastructure.storage import LocalStorage
from invoicedesk.infrastructure.telemetry.logger import configure_logging, get_logger


def run_once(repo, storage, client_factory=LlmClient) -> bool:
    job = repo.claim()
    if not job:
        return False
    stop = threading.Event()
    def heartbeat():
        while not stop.wait(20):
            try:
                Path('/tmp/invoicedesk-worker-heartbeat').touch()
                if not repo.heartbeat(job['id'], job['lease_token']):
                    return
            except Exception:
                return  # lease expiry prevents a stale worker from committing
    thread = threading.Thread(target=heartbeat, daemon=True)
    thread.start()
    def timed_out(signum, frame):
        raise TimeoutError('Worker processing limit exceeded.')
    previous_alarm = signal.signal(signal.SIGALRM, timed_out)
    signal.alarm(180)
    try:
        JobService(repo, storage).process(job, client_factory())
    except Conflict:
        get_logger(__name__).warning('Worker lease lost; result was not committed.')
    except Exception as exc:
        code = 'malformed_document' if isinstance(exc, ValueError) else 'processing_failure'
        repo.fail(job['id'], job['lease_token'], code)
        get_logger(__name__).error('Processing failed: %s', code)
    finally:
        signal.alarm(0)
        signal.signal(signal.SIGALRM, previous_alarm)
        stop.set()
        thread.join(timeout=1)
    return True


def main():
    configure_logging()
    require_production_dependencies()
    from invoicedesk.infrastructure.preflight import require_evaluated_model
    require_evaluated_model(mode='live')
    repo = Repository(os.environ['DATABASE_URL'])
    storage = LocalStorage(Path(os.environ['DOCUMENT_ROOT']))
    while True:
        Path('/tmp/invoicedesk-worker-heartbeat').touch()
        if not run_once(repo, storage, lambda: LlmClient('live')):
            time.sleep(1)


if __name__ == '__main__':
    main()
