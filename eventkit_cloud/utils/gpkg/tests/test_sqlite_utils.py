# -*- coding: utf-8 -*-
import logging
import os
from unittest.mock import Mock, patch

from django.test import TransactionTestCase

from eventkit_cloud.utils.gpkg.sqlite_utils import get_database_connection, Table

logger = logging.getLogger(__name__)


class TestSqliteUtils(TransactionTestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))

    @patch("eventkit_cloud.utils.gpkg.sqlite_utils.connect")
    def test_get_database_connection(self, connect):
        from sqlite3 import Row

        cursor_mock = Mock()
        cursor_mock.fetchone.return_value = "test"
        connect().__enter__().cursor.return_value = cursor_mock

        # Test that a connection object is returned
        with get_database_connection(self.path) as conn:
            self.assertEqual(conn.cursor().fetchone(), "test")

        # Test that the row_factory property is correctly set to sqlite3.Row
        self.assertEqual(get_database_connection(self.path).row_factory, Row)


class TestTableQuery(TransactionTestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))

    def test_get_table_query_validate(self):
        cursor_mock = Mock()

        def fill_cursor(*args):
            if args[1][0] in ["gpkg_contents"]:
                cursor_mock.fetchone.return_value = ("gpkg_contents",)
            else:
                cursor_mock.fetchone.return_value = tuple()

        cursor_mock.execute.side_effect = fill_cursor
        passed = True
        try:
            Table(cursor_mock, "gpkg_contents").validate()
        except ValueError:
            passed = False
        self.assertTrue(passed)
        self.assertRaises(ValueError, Table(cursor_mock, "gpkg_metadata").validate)

        try:
            Table(cursor_mock, "sqlite_master").validate()
        except ValueError:
            passed = False
        self.assertTrue(passed)


class TestTable(TransactionTestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))

    def test_get_table_exists(self):
        cursor_mock = Mock()

        def fill_cursor(*args):
            if args[1][0] in ["gpkg_contents", "other_table"]:
                cursor_mock.fetchone.return_value = ("gpkg_contents",)
            else:
                cursor_mock.fetchone.return_value = tuple()

        cursor_mock.execute.side_effect = fill_cursor
        self.assertTrue(Table.exists(cursor_mock, "gpkg_contents"))
        self.assertTrue(cursor_mock.execute.called_once)
        self.assertTrue(not Table.exists(cursor_mock, "gpkg_metadata"))
        self.assertTrue(Table.exists(cursor_mock, "other_table"))
