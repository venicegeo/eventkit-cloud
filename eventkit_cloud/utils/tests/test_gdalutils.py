# -*- coding: utf-8 -*-
import logging
import os
from uuid import uuid4

from django.test import TestCase
from unittest.mock import Mock, patch, call, MagicMock, ANY
from osgeo import gdal, ogr

from eventkit_cloud.utils.gdalutils import (
    convert,
    is_envelope,
    get_distance,
    get_dimensions,
    merge_geotiffs,
    get_meta,
    get_band_statistics,
    convert_raster,
    convert_vector,
    progress_callback,
    polygonize,
    get_chunked_bbox,
)

logger = logging.getLogger(__name__)


class TestGdalUtils(TestCase):
    def setUp(
        self,
    ):
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.task_process_patcher = patch("eventkit_cloud.utils.gdalutils.TaskProcess")
        self.task_process = self.task_process_patcher.start()
        self.addCleanup(self.task_process_patcher.stop)
        self.task_uid = uuid4()

    @patch("eventkit_cloud.utils.gdalutils.os.path.isfile")
    @patch("eventkit_cloud.utils.gdalutils.open_dataset")
    def test_get_meta(self, open_dataset_mock, isfile):

        dataset_path = "/path/to/dataset"
        isfile.return_value = True
        self.task_process.return_value = Mock(exitcode=0)

        mock_open_dataset = Mock(spec=gdal.Dataset)
        mock_open_dataset.RasterCount = 0
        open_dataset_mock.return_value = mock_open_dataset
        mock_open_dataset.GetDriver.return_value.ShortName = "gtiff"
        expected_meta = {"driver": "gtiff", "is_raster": True, "nodata": None}
        returned_meta = get_meta(dataset_path)
        self.assertEqual(expected_meta, returned_meta)

        mock_open_dataset.RasterCount = 2
        mock_open_dataset.GetRasterBand.return_value.GetNoDataValue.return_value = -32768.0
        expected_meta = {"driver": "gtiff", "is_raster": True, "nodata": -32768.0}
        returned_meta = get_meta(dataset_path)
        self.assertEqual(expected_meta, returned_meta)

        mock_open_dataset = Mock(spec=ogr.DataSource)
        open_dataset_mock.return_value = mock_open_dataset
        mock_open_dataset.GetDriver.return_value.GetName.return_value = "gpkg"
        expected_meta = {"driver": "gpkg", "is_raster": False, "nodata": None}
        returned_meta = get_meta(dataset_path)
        self.assertEqual(expected_meta, returned_meta)

        open_dataset_mock.return_value = None
        expected_meta = {"driver": None, "is_raster": None, "nodata": None}
        returned_meta = get_meta(dataset_path)
        self.assertEqual(expected_meta, returned_meta)

    def test_is_envelope(self):
        envelope_gj = """{"type": "MultiPolygon",
            "coordinates": [ [
                [   [0,0],
                    [1,0],
                    [1,1],
                    [0,1],
                    [0,0]
                ]
            ] ]
        }"""
        triangle_gj = """{"type": "MultiPolygon",
            "coordinates": [ [
                [   [0,0],
                    [1,0],
                    [0,1],
                    [0,0]
                ]
            ] ]
        }"""
        non_env_gj = """{"type": "MultiPolygon",
            "coordinates": [ [
                [   [0,0],
                    [1.5,0],
                    [1,1],
                    [0,1],
                    [0,0]
                ]
            ] ]
        }"""
        empty_gj = ""

        self.assertTrue(is_envelope(envelope_gj))
        self.assertFalse(is_envelope(triangle_gj))
        self.assertFalse(is_envelope(non_env_gj))
        self.assertFalse(is_envelope(empty_gj))

    @patch("eventkit_cloud.utils.gdalutils.get_task_command")
    @patch("eventkit_cloud.utils.gdalutils.is_envelope")
    @patch("eventkit_cloud.utils.gdalutils.get_meta")
    @patch("eventkit_cloud.utils.gdalutils.os.path.isfile")
    def test_convert(self, isfile, get_meta_mock, is_envelope_mock, get_task_command_mock):

        isfile.return_value = True

        with self.assertRaises(Exception):
            convert(boundary=None, input_file=None)

        # Raster geopackage
        in_projection = "EPSG:4326"
        out_projection = "EPSG:3857"
        geojson_file = "/path/to/geojson"
        out_dataset = "/path/to/dataset"
        in_dataset = "/path/to/old_dataset"
        driver = "gpkg"
        band_type = gdal.GDT_Byte
        dstalpha = True
        lambda_mock = Mock()
        get_task_command_mock.return_value = lambda_mock
        get_meta_mock.return_value = {"driver": "gpkg", "is_raster": True, "nodata": None}
        is_envelope_mock.return_value = False
        convert(
            boundary=geojson_file,
            input_file=in_dataset,
            output_file=out_dataset,
            driver=driver,
            task_uid=self.task_uid,
            projection=3857,
        )
        get_task_command_mock.assert_called_once_with(
            convert_raster,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            creation_options=None,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=geojson_file,
            src_srs=in_projection,
            dst_srs=out_projection,
            task_uid=self.task_uid,
            translate_params=None,
            warp_params=None,
            use_translate=False,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

        # Geotiff
        driver = "gtiff"
        band_type = None
        dstalpha = True
        get_meta_mock.return_value = {"driver": "gtiff", "is_raster": True, "nodata": None}
        is_envelope_mock.return_value = True  # So, no need for -dstalpha
        convert(
            boundary=geojson_file, input_file=in_dataset, output_file=out_dataset, driver=driver, task_uid=self.task_uid
        )
        get_task_command_mock.assert_called_once_with(
            convert_raster,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            creation_options=None,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=geojson_file,
            src_srs=in_projection,
            dst_srs=in_projection,
            task_uid=self.task_uid,
            translate_params=None,
            warp_params=None,
            use_translate=False,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

        # Geotiff with non-envelope polygon cutline
        is_envelope_mock.return_value = False
        dstalpha = True
        convert(
            boundary=geojson_file, input_file=in_dataset, output_file=out_dataset, driver=driver, task_uid=self.task_uid
        )
        get_task_command_mock.assert_called_once_with(
            convert_raster,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            creation_options=None,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=geojson_file,
            src_srs=in_projection,
            dst_srs=in_projection,
            task_uid=self.task_uid,
            translate_params=None,
            warp_params=None,
            use_translate=False,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

        # Vector
        driver = "gpkg"
        get_meta_mock.return_value = {"driver": "gpkg", "is_raster": False}
        convert(
            boundary=geojson_file, input_file=in_dataset, output_file=out_dataset, driver=driver, task_uid=self.task_uid
        )
        get_task_command_mock.assert_called_once_with(
            convert_vector,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            dataset_creation_options=None,
            layer_creation_options=None,
            src_srs=in_projection,
            dst_srs=in_projection,
            layers=None,
            layer_name=None,
            access_mode="overwrite",
            boundary=geojson_file,
            bbox=None,
            task_uid=self.task_uid,
            distinct_field=None,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

        # Test that extra_parameters are added when converting to NITF.
        driver = "nitf"
        extra_parameters = "-co ICORDS=G"
        in_projection = "EPSG:4326"
        out_projection = "EPSG:3857"
        band_type = None
        dstalpha = True
        get_meta_mock.return_value = {"driver": "gpkg", "is_raster": True}
        convert(
            driver=driver,
            input_file=in_dataset,
            creation_options=extra_parameters,
            output_file=out_dataset,
            task_uid=self.task_uid,
            projection=3857,
        )
        get_task_command_mock.assert_called_once_with(
            convert_raster,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            creation_options=extra_parameters,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=None,
            src_srs=in_projection,
            dst_srs=out_projection,
            task_uid=self.task_uid,
            translate_params=None,
            warp_params=None,
            use_translate=False,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

        # Test converting to a new projection
        driver = "gpkg"
        in_projection = "EPSG:4326"
        out_projection = "EPSG:3857"
        band_type = gdal.GDT_Byte
        dstalpha = True
        get_meta_mock.return_value = {"driver": "gpkg", "is_raster": True}
        convert(driver=driver, input_file=in_dataset, output_file=out_dataset, task_uid=self.task_uid, projection=3857)
        get_task_command_mock.assert_called_once_with(
            convert_raster,
            [in_dataset],
            out_dataset,
            driver=driver,
            config_options=None,
            creation_options=None,
            band_type=band_type,
            dst_alpha=dstalpha,
            boundary=None,
            src_srs=in_projection,
            dst_srs=out_projection,
            task_uid=self.task_uid,
            translate_params=None,
            warp_params=None,
            use_translate=False,
        )
        get_task_command_mock.reset_mock()
        self.task_process().start_process.assert_called_once_with(lambda_mock)
        self.task_process.reset_mock()

    @patch("eventkit_cloud.utils.gdalutils.ogr")
    @patch("eventkit_cloud.utils.gdalutils.gdal")
    def test_polygonize(self, mock_gdal, mock_ogr):
        example_input = "input.tif"
        example_output = "output.geojson"
        dst_layer = Mock()
        mask_band = Mock()
        mock_ogr.GetDriverByName().CreateDataSource().CreateLayer.return_value = dst_layer
        mock_dataset = MagicMock()
        mock_dataset.RasterCount = 4
        mock_gdal.Open.return_value = mock_dataset
        mock_dataset.GetRasterBand.return_value = mask_band
        polygonize(example_input, example_output)
        expected_band = 4
        mock_dataset.GetRasterBand.assert_called_once_with(expected_band)
        mock_gdal.Polygonize.assert_called_once_with(mask_band, mask_band, dst_layer, -1, [])
        mock_gdal.Open.assert_called_once_with(example_input)
        mock_ogr.GetDriverByName.assert_called_with("GeoJSON")
        mock_ogr.GetDriverByName().CreateDataSource.assert_called_with(example_output)
        mock_ogr.GetDriverByName().CreateDataSource().CreateLayer.assert_called_with(example_output)
        mock_dataset.GetRasterBand.reset_mock()

        mock_dataset.RasterCount = 3
        mock_gdal.Open.return_value = mock_dataset
        polygonize(example_input, example_output)
        expected_band = 4
        mock_gdal.Nearblack.assert_called_once_with(ANY, example_input)
        mock_dataset.GetRasterBand.assert_called_once_with(expected_band)
        mock_dataset.GetRasterBand.reset_mock()

        mock_dataset.RasterCount = 2
        mock_gdal.Open.return_value = mock_dataset
        polygonize(example_input, example_output)
        expected_band = 2
        mock_dataset.GetRasterBand.assert_called_once_with(expected_band)
        mock_dataset.GetRasterBand.reset_mock()

        mock_dataset.RasterCount = 1
        mock_gdal.Open.return_value = mock_dataset
        polygonize(example_input, example_output)
        expected_band = 1
        mock_dataset.GetRasterBand.assert_called_once_with(expected_band)
        mock_dataset.GetRasterBand.reset_mock()

    def test_get_distance(
        self,
    ):
        expected_distance = 972.38
        point_a = [-72.377162, 42.218109]
        point_b = [-72.368493, 42.218903]
        distance = get_distance(point_a, point_b)
        self.assertEqual(int(expected_distance), int(distance))

    @patch("eventkit_cloud.utils.gdalutils.get_distance")
    def test_get_dimensions(self, mock_get_distance):
        bbox = [0, 1, 2, 3]
        scale = 10
        expected_dim = (10, 20)
        mock_get_distance.side_effect = [100, 200]
        dim = get_dimensions(bbox, scale)
        mock_get_distance.assert_has_calls(
            [call([bbox[0], bbox[1]], [bbox[2], bbox[1]]), call([bbox[0], bbox[1]], [bbox[0], bbox[3]])]
        )
        self.assertEqual(dim, expected_dim)

        expected_dim = (1, 1)
        mock_get_distance.side_effect = [6, 8]
        dim = get_dimensions(bbox, scale)
        self.assertEqual(dim, expected_dim)

        expected_dim = (1, 5)
        mock_get_distance.side_effect = [9, 50]
        dim = get_dimensions(bbox, scale)
        self.assertEqual(dim, expected_dim)

        expected_dim = (6, 1)
        mock_get_distance.side_effect = [60, 8]
        dim = get_dimensions(bbox, scale)
        self.assertEqual(dim, expected_dim)

    def test_merge_geotiffs(self):
        in_files = ["1.tif", "2.tif", "3.tif", "4.tif"]
        out_file = "merged.tif"
        task_uid = "1234"
        result = merge_geotiffs(in_files, out_file, task_uid=task_uid)
        self.task_process.start_process.called_once()
        self.assertEqual(out_file, result)

        with self.assertRaises(Exception):
            self.task_process().start_process.side_effect = Exception("Error")
            merge_geotiffs(in_files, out_file, task_uid=task_uid)

    @patch("eventkit_cloud.utils.gdalutils.gdal")
    def test_get_band_statistics(self, mock_gdal):
        in_file = "test.tif"
        example_stats = [0, 10, 5, 2]
        mock_gdal.Open.return_value.GetRasterBand.return_value.GetStatistics.return_value = example_stats
        returned_stats = get_band_statistics(in_file)
        self.assertEqual(example_stats, returned_stats)
        mock_gdal.Open.assert_called_once_with(in_file)

        mock_gdal.Open.return_value.GetRasterBand.return_value.GetStatistics.side_effect = [Exception]
        self.assertIsNone(get_band_statistics(in_file))

    @patch("eventkit_cloud.utils.gdalutils.update_progress")
    def test_progress_callback(self, mock_update_progress):
        example_percentage = 0.10
        example_message = "message"
        task_uid = "123"
        subtask_percentage = 100
        example_user_data = {"task_uid": task_uid, "subtask_percentage": subtask_percentage}
        progress_callback(example_percentage, example_message, example_user_data)
        mock_update_progress.assert_called_once_with(
            task_uid, progress=10, subtask_percentage=subtask_percentage, msg=example_message
        )

    @patch("eventkit_cloud.utils.gdalutils.get_dataset_names")
    @patch("eventkit_cloud.utils.gdalutils.gdal")
    def test_convert_raster(self, mock_gdal, mock_get_dataset_names):
        task_uid = "123"
        input_file = "/test/test.gpkg"
        output_file = "/test/test.tif"
        boundary = "/test/test.json"
        driver = "gtiff"
        srs = "EPSG:4326"

        mock_get_dataset_names.return_value = (input_file, output_file)
        convert_raster(
            input_file, output_file, driver=driver, boundary=boundary, src_srs=srs, dst_srs=srs, task_uid=task_uid
        )
        mock_gdal.Warp.assert_called_once_with(
            output_file,
            [input_file],
            callback=progress_callback,
            callback_data={"task_uid": task_uid, "subtask_percentage": 50},
            cropToCutline=True,
            cutlineDSName=boundary,
            dstSRS=srs,
            format=driver,
            srcSRS=srs,
        )
        mock_gdal.Translate.assert_called_once_with(
            output_file,
            input_file,
            callback=progress_callback,
            format=driver,
            callback_data={"task_uid": task_uid, "subtask_percentage": 50},
            creationOptions=["COMPRESS=LZW", "TILED=YES", "BIGTIFF=YES"],
        )
        mock_gdal.reset_mock()
        warp_params = {"warp": "params"}
        translate_params = {"translate": "params"}
        convert_raster(
            input_file,
            output_file,
            driver=driver,
            boundary=boundary,
            src_srs=srs,
            dst_srs=srs,
            task_uid=task_uid,
            warp_params=warp_params,
            translate_params=translate_params,
        )
        mock_gdal.Warp.assert_called_once_with(
            output_file,
            [input_file],
            callback=progress_callback,
            callback_data={"task_uid": task_uid, "subtask_percentage": 50},
            cropToCutline=True,
            cutlineDSName=boundary,
            format=driver,
            warp="params",
        )
        mock_gdal.Translate.assert_called_once_with(
            output_file,
            input_file,
            callback=progress_callback,
            format=driver,
            callback_data={"task_uid": task_uid, "subtask_percentage": 50},
            translate="params",
        )

    @patch("eventkit_cloud.utils.gdalutils.gdal")
    def test_convert_vector(self, mock_gdal):
        task_uid = "123"
        input_file = "/test/test.gpkg"
        output_file = "/test/test.kml"
        boundary = "/test/test.json"
        driver = "kml"
        src_srs = "EPSG:4326"
        dst_srs = "EPSG:3857"

        convert_vector(
            input_file,
            output_file,
            driver=driver,
            boundary=boundary,
            src_srs=src_srs,
            dst_srs=dst_srs,
            task_uid=task_uid,
        )
        mock_gdal.VectorTranslate.assert_called_once_with(
            output_file,
            input_file,
            accessMode="overwrite",
            callback=progress_callback,
            callback_data={"task_uid": task_uid},
            dstSRS=dst_srs,
            format=driver,
            options=["-clipSrc", boundary],
            reproject=True,
            skipFailures=True,
            srcSRS=src_srs,
        )

    @patch("eventkit_cloud.utils.gdalutils.gdal")
    def test_get_chunked_tiles(self, mock_gdal):
        bbox = [-77.26290092220698, 38.58181863431442, -76.86362334675664, 38.94635628150632]
        bboxes = get_chunked_bbox(bbox)
        self.assertEqual(len(bboxes), 6)
        self.assertEqual(bboxes[0], (-77.26290092220698, 38.76408745791032, -77.08063209861098, 38.94635628150632))
