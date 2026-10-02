import json
import unittest
from unittest.mock import patch
import httpx
from openai import OpenAI
from token_budget_mcp.core import OpenAIProvider

class ProviderTests(unittest.TestCase):
    def test_sdk_payload_and_usage(self):
        seen=[]
        def handle(request):
            data=json.loads(request.content)
            seen.append((request.url.path,data))
            if request.url.path.endswith('/input_tokens'):
                return httpx.Response(200,json={'object':'response.input_tokens','input_tokens':33})
            return httpx.Response(200,json={'id':'resp_test','object':'response','created_at':0,
                'model':'gpt-4.1-mini','status':'completed','output':[{'id':'msg_test','type':'message',
                'role':'assistant','status':'completed','content':[{'type':'output_text','text':'Singapore','annotations':[]}]}],
                'usage':{'input_tokens':33,'output_tokens':4,'total_tokens':37,
                         'input_tokens_details':{'cached_tokens':10},
                         'output_tokens_details':{'reasoning_tokens':0}}})
        client=OpenAI(api_key='test-only',max_retries=0,http_client=httpx.Client(transport=httpx.MockTransport(handle)))
        with patch('openai.OpenAI',return_value=client):
            provider=OpenAIProvider()
        messages=[{'role':'user','content':'Extract Singapore'}]
        self.assertEqual(provider.count('gpt-4.1-mini',messages),(33,'provider_count'))
        answer=provider.generate('gpt-4.1-mini',messages,128)
        self.assertEqual(answer['answer'],'Singapore')
        self.assertEqual(answer['usage']['total_tokens'],37)
        self.assertEqual(seen[1][1]['input'],messages)
        self.assertEqual(seen[1][1]['max_output_tokens'],128)
        self.assertFalse(seen[1][1]['store'])
        self.assertEqual(seen[1][1]['service_tier'],'default')
        client.close()
