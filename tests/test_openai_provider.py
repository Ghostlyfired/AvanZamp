import base64
from io import BytesIO
import json
from urllib.error import HTTPError, URLError
import pytest
from invoicedesk.config import load_environment
from invoicedesk.policy import ROOT
from invoicedesk.extract.llm import OUTPUT_FIELDS
from invoicedesk.models import ExtractedInvoice
from invoicedesk.extract.providers import load_provider, ProviderUnavailable
from invoicedesk.extract.providers import openai as adapter


def schema():
    result=ExtractedInvoice.model_json_schema()
    result['properties']={key:value for key,value in result['properties'].items() if key in OUTPUT_FIELDS}
    return result


def completed_response():
    return {'status':'completed','output':[{'type':'message','content':[{
        'type':'output_text','text':json.dumps({'invoice_number':'TEST-1'})}]}],
        'usage':{'input_tokens':42,'output_tokens':12}}


@pytest.mark.parametrize('filename,block_type', [('office_furniture.png','input_image'),('office_furniture.pdf','input_file')])
def test_openai_request_and_response(monkeypatch,filename,block_type):
    monkeypatch.setenv('OPENAI_API_KEY','test-key-not-real')
    requests=[]
    def fake_urlopen(request,*,timeout):
        assert request.full_url=='https://api.openai.com/v1/responses'
        assert timeout==90
        requests.append(json.loads(request.data))
        return BytesIO(json.dumps(completed_response()).encode())
    monkeypatch.setattr(adapter,'urlopen',fake_urlopen)
    result=load_provider('openai').extract(ROOT/'data/examples'/filename,model='gpt-5-mini',schema=schema())
    request=requests[0]
    assert request['model']=='gpt-5-mini' and request['store'] is False
    assert request['input'][0]['content'][0]['type']==block_type
    assert request['text']['format']['strict'] is True
    assert result.extraction['invoice_number']=='TEST-1'
    assert result.usage['input_tokens']==42


def test_strict_schema_is_closed_and_uses_decimal_strings():
    result=adapter.response_schema(schema())
    assert result['additionalProperties'] is False
    assert result['properties']['evidence']['additionalProperties'] is False
    assert set(result['properties']['evidence']['properties'])==set(OUTPUT_FIELDS)-{'evidence'}
    assert result['properties']['total']['anyOf'][0]['type']=='string'
    assert 'pattern' in result['properties']['total']['anyOf'][0]
    assert result['properties']['total']['anyOf'][1]=={'type':'null'}
    assert all(field == {'type':'string'} for field in result['properties']['evidence']['properties'].values())
    item=result['$defs']['LineItem']
    assert item['additionalProperties'] is False
    assert set(item['required'])==set(item['properties'])


@pytest.mark.parametrize('body',[
    {'status':'incomplete','output':[]},
    {'status':'completed','output':[{'type':'message','content':[{'type':'refusal','refusal':'No'}]}]},
    {'status':'completed','output':[]},
])
def test_incomplete_refused_empty_responses_fail_safely(monkeypatch,body):
    monkeypatch.setenv('OPENAI_API_KEY','test-key')
    monkeypatch.setattr(adapter,'urlopen',lambda *a,**kw:BytesIO(json.dumps(body).encode()))
    with pytest.raises(ProviderUnavailable) as error:
        adapter.OpenAIProvider().extract(ROOT/'data/examples/office_furniture.pdf',model='gpt-5-mini',schema=schema())
    assert error.value.api_calls==1


def test_http_errors_do_not_expose_credentials(monkeypatch):
    dummy='test-key-do-not-log'
    monkeypatch.setenv('OPENAI_API_KEY',dummy)
    def failure(*args,**kwargs):
        body=json.dumps({'error':{'code':'invalid_api_key','message':'Bad key '+dummy}}).encode()
        raise HTTPError(adapter.ENDPOINT,401,'Unauthorized',{},BytesIO(body))
    monkeypatch.setattr(adapter,'urlopen',failure)
    with pytest.raises(ProviderUnavailable) as error:
        adapter.OpenAIProvider().extract(ROOT/'data/examples/office_furniture.pdf',model='gpt-5-mini',schema=schema())
    assert dummy not in str(error.value) and '401' in str(error.value)


def test_dotenv_quotes_comments_and_shell_precedence(tmp_path,monkeypatch):
    path=tmp_path/'.env'
    path.write_text('OPENAI_API_KEY="fake#key"\nLLM_PROVIDER=openai\nLLM_MODEL=gpt-5-mini # comment\n')
    monkeypatch.delenv('OPENAI_API_KEY',raising=False)
    monkeypatch.delenv('LLM_PROVIDER',raising=False)
    monkeypatch.setenv('LLM_MODEL','shell-model')
    load_environment(path)
    import os
    assert os.getenv('OPENAI_API_KEY')=='fake#key'
    assert os.getenv('LLM_PROVIDER')=='openai'
    assert os.getenv('LLM_MODEL')=='shell-model'


def test_bad_dotenv_does_not_echo_value(tmp_path):
    path=tmp_path/'.env';path.write_text('OPENAI_API_KEY="private-unclosed')
    with pytest.raises(ValueError) as error:load_environment(path)
    assert 'private-unclosed' not in str(error.value)

def test_cli_loads_dotenv_provider_and_model(tmp_path,monkeypatch,capsys):
    from invoicedesk.cli import main
    for name in ['LLM_MODE','LLM_PROVIDER','LLM_MODEL','OPENAI_API_KEY']:
        monkeypatch.delenv(name,raising=False)
    envfile=tmp_path/'.env'
    envfile.write_text('LLM_MODE=live\nLLM_PROVIDER=openai\nLLM_MODEL=gpt-5-mini\nOPENAI_API_KEY=offline-test-key\n')
    monkeypatch.setenv('INVOICEDESK_ENV_FILE',str(envfile))
    monkeypatch.setattr('invoicedesk.extract.llm.ROOT',tmp_path)
    payload=completed_response()
    payload['output'][0]['content'][0]['text']=json.dumps({
        'vendor_name':'Northwind Office Supplies Ltd','invoice_number':'TEST-1',
        'invoice_date':'2026-11-09','currency':'USD','po_reference':'PO-1001',
        'subtotal':'100.00','tax_amount':'8.00','total':'108.00','evidence':{}})
    requests=[]
    def fake_request(request,**kwargs):
        requests.append(json.loads(request.data))
        return BytesIO(json.dumps(payload).encode())
    monkeypatch.setattr(adapter,'urlopen',fake_request)
    assert main(['--db',str(tmp_path/'db.sqlite3'),'run',str(ROOT/'data/examples/office_furniture.png'),'--json'])==0
    result=json.loads(capsys.readouterr().out)
    assert requests[0]['model']=='gpt-5-mini'
    assert result['extraction']['invoice_number']=='TEST-1'
    extraction=next(e for e in result['events'] if e['stage']=='extract' and e['state']=='completed')
    assert extraction['payload']['provider']=='openai'


def test_make_respects_dotenv_and_explicit_mode(tmp_path,monkeypatch):
    import os
    import shutil
    if os.name=='nt' or not shutil.which('make'):
        pytest.skip('Make integration requires a POSIX shell; provider/CLI tests run separately.')
    import os
    import subprocess
    for name in ['LLM_MODE','LLM_PROVIDER','LLM_MODEL']:
        monkeypatch.delenv(name,raising=False)
    envfile=tmp_path/'config.env'
    envfile.write_text('LLM_MODE=off\nLLM_PROVIDER=openai\nLLM_MODEL=gpt-5-mini\n')
    monkeypatch.setenv('INVOICEDESK_ENV_FILE',str(envfile))
    for overrides in [[],['LLM_MODE=cache']]:
        result=subprocess.run(['make','run','FILE=data/examples/office_furniture.png',
            f'DB={tmp_path/"make.sqlite3"}',*overrides],cwd=ROOT,env=os.environ.copy(),capture_output=True,text=True,timeout=20)
        assert result.returncode==0,result.stderr
        expected='cache' if overrides else 'off'
        assert f'Extraction mode: {expected}' in result.stderr
        assert 'provider: openai | model: gpt-5-mini' in result.stderr
