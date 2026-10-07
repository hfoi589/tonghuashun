from datetime import datetime, timezone, timedelta
from level2_service.request_logs import RequestLogEntry, RequestLogStore

def test_request_log_store_records_and_filters_without_sensitive_fields(tmp_path):
    store = RequestLogStore(tmp_path/'app.db')
    store.record(RequestLogEntry(timestamp=datetime.now(timezone.utc), ip='1.2.3.4', device_type='mobile', user_agent='UA', path='/api/v1/jobs', action='job_submit', symbol='601872', stock_name='招商轮船', status_code=200, task_status='COMPLETED', error_code=None, duration_ms=12.5, public_id='job1'))
    items,total=store.list(limit=10,offset=0,filters={"symbol":"601872"})
    assert total==1 and items[0].stock_name=='招商轮船'
    assert not hasattr(items[0], 'request_body')


def test_request_log_store_persists_sanitized_error_detail(tmp_path):
    store = RequestLogStore(tmp_path / 'app.db')
    store.record(RequestLogEntry(
        timestamp=datetime.now(timezone.utc),
        path='/api/v1/symbols/601872',
        action='symbol_lookup',
        status_code=503,
        error_code='HTTP_503',
        error_detail='SYMBOL_CATALOG_STALE',
    ))

    items, total = store.list(limit=10, offset=0)
    assert total == 1
    assert items[0].error_detail == 'SYMBOL_CATALOG_STALE'

def test_request_log_store_purges_old_rows(tmp_path):
    store=RequestLogStore(tmp_path/'app.db')
    store.record(RequestLogEntry(timestamp=datetime.now(timezone.utc)-timedelta(days=91), ip='1', device_type='desktop', user_agent='u', path='/api/v1/symbols/601872', action='symbol_lookup', symbol='601872', stock_name='x', status_code=200, task_status=None, error_code=None, duration_ms=1, public_id=None))
    assert store.purge_before(datetime.now(timezone.utc)-timedelta(days=90))==1


def test_request_log_uses_exact_local_identity_when_fresh_catalog_lookup_is_stale(tmp_path):
    from fastapi.testclient import TestClient

    from level2_service.api import create_app
    from level2_service.parsed_values import DirectRequestError, SymbolLookup

    class Catalog:
        @staticmethod
        def startup_refresh_required():
            return False

        @staticmethod
        def lookup_existing(symbol):
            return SymbolLookup(symbol=symbol, name='招商轮船', market='17')

    def stale_lookup(_symbol):
        raise DirectRequestError('SYMBOL_CATALOG_STALE')

    store = RequestLogStore(tmp_path / 'app.db')
    app = create_app(
        symbol_lookup=stale_lookup,
        symbol_catalog=Catalog(),
        request_log_store=store,
    )

    with TestClient(app) as client:
        response = client.get('/api/v1/symbols/601872')

    assert response.status_code == 503
    items, total = store.list(limit=10, offset=0, filters={'symbol': '601872'})
    assert total == 1
    assert items[0].stock_name == '招商轮船'
    assert items[0].error_detail == 'SYMBOL_CATALOG_STALE'


def test_request_log_store_backfills_only_missing_names(tmp_path):
    store = RequestLogStore(tmp_path / 'app.db')
    for symbol, stock_name in (
        ('601872', None),
        ('000001', None),
        ('002202', '原有名称'),
    ):
        store.record(
            RequestLogEntry(
                timestamp=datetime.now(timezone.utc),
                path=f'/api/v1/market/symbols/{symbol}/snapshot',
                action='market_tab',
                symbol=symbol,
                stock_name=stock_name,
            )
        )

    updated = store.backfill_stock_names(
        lambda symbol: {'601872': '招商轮船', '000001': '平安银行'}.get(symbol)
    )

    assert updated == 2
    rows, total = store.list(limit=10, offset=0)
    assert total == 3
    assert {row.symbol: row.stock_name for row in rows} == {
        '601872': '招商轮船',
        '000001': '平安银行',
        '002202': '原有名称',
    }
