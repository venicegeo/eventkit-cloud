# -*- coding: utf-8 -*-
import logging
import os
from uuid import uuid4

from django.test import TransactionTestCase
from unittest.mock import Mock, patch, call

from eventkit_cloud.utils.geopackage import (
    add_geojson_to_geopackage,
    get_table_count,
    get_table_names,
    get_tile_table_names,
    get_table_gpkg_contents_information,
    set_gpkg_contents_bounds,
    get_zoom_levels_table,
    remove_empty_zoom_levels,
    remove_zoom_level,
    get_tile_matrix_table_zoom_levels,
    check_content_exists,
    check_zoom_levels,
    get_table_info,
    create_table_from_existing,
    create_metadata_tables,
    create_extension_table,
    add_file_metadata,
)


logger = logging.getLogger(__name__)


class TestGeopackage(TransactionTestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.task_process_patcher = patch("eventkit_cloud.utils.geopackage.TaskProcess")
        self.task_process = self.task_process_patcher.start()
        self.addCleanup(self.task_process_patcher.stop)
        self.task_uid = uuid4()

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_table_count(self, sqlite3):
        expected_count = 4
        gpkg = "/test/file.gpkg"
        cursor_mock = Mock()
        cursor_mock.fetchone.return_value = [expected_count]
        sqlite3.connect().__enter__().execute.return_value = cursor_mock

        bad_table_name = "test;this"
        returned_count = get_table_count(gpkg, bad_table_name)
        sqlite3.connect().__enter__().execute.assert_not_called()
        self.assertFalse(returned_count)

        table_name = "test"
        returned_count = get_table_count(gpkg, table_name)
        sqlite3.connect().__enter__().execute.assert_called_once_with("SELECT COUNT(*) FROM '{0}';".format(table_name))
        self.assertEqual(expected_count, returned_count)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_table_names(self, sqlite3):
        expected_table_names = ["test1", "test2"]
        mock_table_names = [(expected_table_names[0],), (expected_table_names[1],)]
        gpkg = "/test/file.gpkg"
        sqlite3.connect().__enter__().execute.return_value = mock_table_names

        return_table_names = get_table_names(gpkg)
        sqlite3.connect().__enter__().execute.assert_called_once_with("SELECT table_name FROM gpkg_contents;")
        self.assertEqual(expected_table_names, return_table_names)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_tile_table_names(self, sqlite3):
        expected_table_names = ["test1", "test2"]
        mock_table_names = [(expected_table_names[0],), (expected_table_names[1],)]
        gpkg = "/test/file.gpkg"
        sqlite3.connect().__enter__().execute.return_value = mock_table_names

        return_table_names = get_tile_table_names(gpkg)
        sqlite3.connect().__enter__().execute.assert_called_once_with(
            "SELECT table_name FROM gpkg_contents WHERE data_type = 'tiles';"
        )
        self.assertEqual(expected_table_names, return_table_names)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_set_gpkg_contents_bounds(self, sqlite3):
        table_name = "test1"
        gpkg = "/test/file.gpkg"
        bbox = [-1, 0, 2, 1]

        sqlite3.connect().__enter__().execute.return_value = Mock(rowcount=1)
        set_gpkg_contents_bounds(gpkg, table_name, bbox)
        sqlite3.connect().__enter__().execute.assert_called_once_with(
            "UPDATE gpkg_contents SET min_x = {0}, min_y = {1}, max_x = {2}, max_y = {3} "
            "WHERE table_name = '{4}';".format(bbox[0], bbox[1], bbox[2], bbox[3], table_name)
        )

        with self.assertRaises(Exception):
            sqlite3.connect().__enter__().execute.return_value = Mock(rowcount=0)
            set_gpkg_contents_bounds(gpkg, table_name, bbox)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_zoom_levels_table(self, sqlite3):
        expected_zoom_levels = [0, 1, 2]
        mock_zoom_levels = [(expected_zoom_levels[0],), (expected_zoom_levels[1],), (expected_zoom_levels[2],)]
        gpkg = "/test/file.gpkg"
        sqlite3.connect().__enter__().execute.return_value = mock_zoom_levels

        bad_table_name = "test;this"
        returned_zoom_levels = get_zoom_levels_table(gpkg, bad_table_name)
        sqlite3.connect().__enter__().execute.assert_not_called()
        self.assertFalse(returned_zoom_levels)

        table_name = "test"
        returned_zoom_levels = get_zoom_levels_table(gpkg, table_name)
        sqlite3.connect().__enter__().execute.assert_called_once_with(
            "SELECT DISTINCT zoom_level FROM '{0}';".format(table_name)
        )
        self.assertEqual(expected_zoom_levels, returned_zoom_levels)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_remove_zoom_level(self, sqlite3):
        zoom_level = 1
        gpkg = "/test/file.gpkg"

        with self.assertRaises(Exception):
            bad_table_name = "test;this"
            remove_zoom_level(gpkg, bad_table_name, zoom_level)

        table_name = "test"
        with self.assertRaises(Exception):
            sqlite3.connect().__enter__().execute.return_value = Mock(rowcount=0)
            remove_zoom_level(gpkg, table_name, zoom_level)

        sqlite3.reset_mock()
        sqlite3.connect().__enter__().execute.return_value = Mock(rowcount=1)
        remove_zoom_level(gpkg, table_name, zoom_level)
        sqlite3.connect().__enter__().execute.assert_called_once_with(
            "DELETE FROM gpkg_tile_matrix WHERE table_name = '{0}' AND zoom_level = '{1}';".format(
                table_name, int(zoom_level)
            )
        )

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_tile_matrix_table_zoom_levels(self, sqlite3):
        expected_zoom_levels = [0, 1, 2]
        mock_zoom_levels = [(expected_zoom_levels[0],), (expected_zoom_levels[1],), (expected_zoom_levels[2],)]
        gpkg = "/test/file.gpkg"
        sqlite3.connect().__enter__().execute.return_value = mock_zoom_levels

        bad_table_name = "test;this"
        returned_zoom_levels = get_tile_matrix_table_zoom_levels(gpkg, bad_table_name)
        sqlite3.connect().__enter__().execute.assert_not_called()
        self.assertFalse(returned_zoom_levels)

        table_name = "test"
        returned_zoom_levels = get_tile_matrix_table_zoom_levels(gpkg, table_name)
        sqlite3.connect().__enter__().execute.assert_called_once_with(
            "SELECT zoom_level FROM gpkg_tile_matrix WHERE table_name = '{0}';".format(table_name)
        )
        self.assertEqual(expected_zoom_levels, returned_zoom_levels)

    @patch("eventkit_cloud.utils.geopackage.remove_zoom_level")
    @patch("eventkit_cloud.utils.geopackage.get_tile_matrix_table_zoom_levels")
    @patch("eventkit_cloud.utils.geopackage.get_zoom_levels_table")
    @patch("eventkit_cloud.utils.geopackage.get_tile_table_names")
    def test_remove_empty_zoom_levels(
        self, get_tile_table_names, get_zoom_levels_table, get_tile_matrix_table_zoom_levels, remove_zoom_level
    ):
        gpkg = "/test/file.gpkg"
        table_names = ["test_1", "test_2"]
        populated_zoom_levels = [1, 2, 3]
        tile_matrix_zoom_levels = [1, 2, 3, 4]
        get_tile_table_names.return_value = table_names
        get_zoom_levels_table.return_value = populated_zoom_levels
        get_tile_matrix_table_zoom_levels.return_value = tile_matrix_zoom_levels

        remove_empty_zoom_levels(gpkg)
        get_tile_table_names.assert_called_once_with(gpkg)
        self.assertEqual([call(gpkg, table_names[0]), call(gpkg, table_names[1])], get_zoom_levels_table.mock_calls)
        self.assertEqual(
            [call(gpkg, table_names[0]), call(gpkg, table_names[1])], get_tile_matrix_table_zoom_levels.mock_calls
        )
        self.assertEqual([call(gpkg, table_names[0], 4), call(gpkg, table_names[1], 4)], remove_zoom_level.mock_calls)

    @patch("eventkit_cloud.utils.geopackage.get_table_count")
    @patch("eventkit_cloud.utils.geopackage.get_table_names")
    def test_check_content_exists(self, get_table_names, get_table_count):
        gpkg = "/test/file.gpkg"
        table_names = ["test_1", "test_2"]
        get_table_names.return_value = table_names

        get_table_count.return_value = 0
        returned_value = check_content_exists(gpkg)
        self.assertFalse(returned_value)

        get_table_count.return_value = 1
        returned_value = check_content_exists(gpkg)
        self.assertTrue(returned_value)

        self.assertEqual([call(gpkg), call(gpkg)], get_table_names.mock_calls)

    @patch("eventkit_cloud.utils.geopackage.get_zoom_levels_table")
    @patch("eventkit_cloud.utils.geopackage.get_tile_matrix_table_zoom_levels")
    @patch("eventkit_cloud.utils.geopackage.get_table_names")
    def test_check_zoom_levels(self, get_table_names, get_tile_matrix_table_zoom_levels, get_zoom_levels_table):
        gpkg = "/test/file.gpkg"
        table_names = ["test_1", "test_2"]
        get_table_names.return_value = table_names

        get_tile_matrix_table_zoom_levels.return_value = [0, 1, 2, 3]
        get_zoom_levels_table.return_value = [0, 1, 2]
        returned_value = check_zoom_levels(gpkg)
        self.assertFalse(returned_value)

        get_tile_matrix_table_zoom_levels.return_value = [0, 1, 2]
        get_zoom_levels_table.return_value = [0, 1, 2]
        returned_value = check_zoom_levels(gpkg)
        self.assertTrue(returned_value)

        self.assertEqual([call(gpkg), call(gpkg)], get_table_names.mock_calls)

    @patch("eventkit_cloud.utils.gdalutils.convert")
    @patch("builtins.open")
    def test_add_geojson_to_geopackage(self, open, mock_convert):

        geojson = "{}"
        gpkg = None
        with self.assertRaises(Exception):
            add_geojson_to_geopackage(geojson=geojson, gpkg=gpkg)

        geojson = "{}"
        gpkg = "test.gpkg"
        layer_name = "test_layer"
        add_geojson_to_geopackage(geojson=geojson, gpkg=gpkg, layer_name=layer_name, task_uid=self.task_uid)
        open.assert_called_once_with(
            os.path.join(os.path.dirname(gpkg), "{0}.geojson".format(os.path.splitext(os.path.basename(gpkg))[0])), "w"
        )
        gdal_mock = mock_convert.return_value
        gdal_mock.convert.called_once_with(
            driver="gpkg", input_file=geojson, output_file=gpkg, creation_options="-nln {0}".format(layer_name)
        )

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    @patch("eventkit_cloud.utils.geopackage.get_table_info")
    def test_create_table_from_existing(self, mock_table_info, mock_sqlite3):
        table_info = [(1, "col1", "INT", 0, 0, 1), (1, "col2", "INT", 0, 0, 0)]
        mock_table_info.return_value = table_info
        gpkg = "test.gpkg"
        old_table = "old"
        new_table = "new"
        create_table_from_existing(gpkg, old_table, new_table)
        mock_table_info.assert_called_once_with(gpkg, old_table)
        mock_sqlite3.connect().__enter__().execute.assert_called_once_with(
            "CREATE TABLE {0} (col1 INTEGER PRIMARY KEY AUTOINCREMENT,col2 INT);".format(new_table)
        )

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_table_info(self, mock_sqlite3):
        gpkg = "test.gpkg"
        table = "table"
        expected_response = [(1, "col1", "INT", 0, 0, 1), (1, "col2", "INT", 0, 0, 0)]
        mock_sqlite3.connect().__enter__().execute.return_value = expected_response
        response = get_table_info(gpkg, table)
        mock_sqlite3.connect().__enter__().execute.assert_called_once_with("PRAGMA table_info({0});".format(table))
        self.assertEqual(expected_response, response)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_get_table_gpkg_contents_information(self, mock_sqlite3):
        gpkg = "test.gpkg"
        table = "table"
        query_response = (table, "tiles", "identifier", "description", "datetime", -180, -90, 190, 90, 4326)
        expected_response = {
            "table_name": query_response[0],
            "data_type": query_response[1],
            "identifier": query_response[2],
            "description": query_response[3],
            "last_change": query_response[4],
            "min_x": query_response[5],
            "min_y": query_response[6],
            "max_x": query_response[7],
            "max_y": query_response[8],
            "srs_id": query_response[9],
        }
        mock_sqlite3.connect().__enter__().execute().fetchone.return_value = query_response
        response = get_table_gpkg_contents_information(gpkg, table)
        mock_sqlite3.connect().__enter__().execute.assert_called_with(
            "SELECT table_name, data_type, identifier, description, last_change, min_x, min_y, max_x, max_y, srs_id "
            "FROM gpkg_contents WHERE table_name = '{0}';".format(table)
        )
        self.assertEqual(expected_response, response)

    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_create_extension_table(self, mock_sqlite3):
        gpkg = "test.gpkg"
        create_extension_table(gpkg)
        mock_sqlite3.connect().__enter__().execute.assert_called_once()

    @patch("eventkit_cloud.utils.geopackage.create_extension_table")
    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_create_metadata_tables(self, mock_sqlite3, mock_create_extension_table):
        gpkg = "test.gpkg"
        create_metadata_tables(gpkg)
        mock_sqlite3.connect().__enter__().execute.assert_called()
        mock_create_extension_table.assert_called_once_with(gpkg)

    @patch("eventkit_cloud.utils.geopackage.create_metadata_tables")
    @patch("eventkit_cloud.utils.geopackage.sqlite3")
    def test_add_file_metadata(self, mock_sqlite3, mock_create_metadata_tables):
        gpkg = "test.gpkg"
        metadata = "sample data"
        add_file_metadata(gpkg, metadata)
        mock_sqlite3.connect().__enter__().execute.assert_called()
        mock_create_metadata_tables.assert_called_once_with(gpkg)
