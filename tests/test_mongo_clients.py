from unittest import TestCase
from unittest.mock import patch

from database import mongo_clients


class MongoClientsTest(TestCase):
    def test_shares_pools_only_for_identical_configured_uris(self):
        mongo_clients._open_client.cache_clear()
        self.addCleanup(mongo_clients._open_client.cache_clear)
        one, two = object(), object()
        with patch.object(mongo_clients, 'AsyncIOMotorClient', side_effect=[one, two]) as constructor:
            self.assertIs(mongo_clients.mongo_client('mongodb://first'), one)
            self.assertIs(mongo_clients.mongo_client(' mongodb://first '), one)
            self.assertIs(mongo_clients.mongo_client('mongodb://second'), two)
        self.assertEqual(constructor.call_count, 2)
