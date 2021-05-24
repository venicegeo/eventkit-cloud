# -*- coding: utf-8 -*-
import logging
import os
from string import Template

import json
from django.conf import settings
from django.contrib.gis.geos import Polygon

from eventkit_cloud.tasks.task_process import TaskProcess, update_progress  # NOQA
from osgeo import gdal, osr
import sqlite3

from .artifact import Artifact
from eventkit_cloud.feature_selection.feature_selection import slugify
from eventkit_cloud.utils import gdalutils


logger = logging.getLogger(__name__)

SPATIAL_SQL = """
UPDATE 'points' SET geom=GeomFromGPB(geom);
UPDATE 'lines' SET geom=GeomFromGPB(geom);
UPDATE 'multipolygons' SET geom=GeomFromGPB(geom);

UPDATE points SET geom = (SELECT ST_Intersection(boundary.geom,p.geom) FROM boundary,points p WHERE points.fid = p.fid);
UPDATE lines SET geom = (SELECT ST_Intersection(boundary.geom,l.geom) FROM boundary,lines l WHERE lines.fid = l.fid);
UPDATE multipolygons SET geom = (SELECT ST_Intersection(boundary.geom,m.geom) FROM boundary,multipolygons m
WHERE multipolygons.fid = m.fid);

DELETE FROM points where geom IS NULL;
DELETE FROM lines where geom IS NULL;
DELETE FROM multipolygons where geom IS NULL;

DROP TRIGGER rtree_multipolygons_geom_delete;
DROP TRIGGER rtree_multipolygons_geom_insert;
DROP TRIGGER rtree_multipolygons_geom_update1;
DROP TRIGGER rtree_multipolygons_geom_update2;
DROP TRIGGER rtree_multipolygons_geom_update3;
DROP TRIGGER rtree_multipolygons_geom_update4;
DROP TRIGGER rtree_points_geom_delete;
DROP TRIGGER rtree_points_geom_insert;
DROP TRIGGER rtree_points_geom_update1;
DROP TRIGGER rtree_points_geom_update2;
DROP TRIGGER rtree_points_geom_update3;
DROP TRIGGER rtree_points_geom_update4;
DROP TRIGGER rtree_lines_geom_delete;
DROP TRIGGER rtree_lines_geom_insert;
DROP TRIGGER rtree_lines_geom_update1;
DROP TRIGGER rtree_lines_geom_update2;
DROP TRIGGER rtree_lines_geom_update3;
DROP TRIGGER rtree_lines_geom_update4;

-- TODO: these are invalid multipolygons that result in GeometryCollections of linear features.
-- see https://github.com/hotosm/osm-export-tool2/issues/155 for discussion.
-- maybe we should log these somewhere.
DELETE FROM multipolygons where GeometryType(geom) NOT IN ('POLYGON','MULTIPOLYGON');

SELECT gpkgAddSpatialIndex('boundary', 'geom');

UPDATE 'boundary' SET geom=AsGPB(geom);
UPDATE 'points' SET geom=AsGPB(geom);
UPDATE 'lines' SET geom=AsGPB(geom);
UPDATE 'multipolygons' SET geom=AsGPB(geom);

DROP TABLE multilinestrings;
DROP TABLE other_relations;
DROP TABLE rtree_lines_geom;
DROP TABLE rtree_multilinestrings_geom;
DROP TABLE rtree_multipolygons_geom;
DROP TABLE rtree_other_relations_geom;
DROP TABLE rtree_points_geom;

INSERT INTO gpkg_contents VALUES ('boundary', 'features', 'boundary', '', '2017-04-08T01:35:16.576Z', null, null,
null, null, '4326');
INSERT INTO gpkg_geometry_columns VALUES ('boundary', 'geom', 'MULTIPOLYGON', '4326', '0', '0');
DELETE FROM gpkg_contents WHERE table_name="multilinestrings";
DELETE FROM gpkg_geometry_columns WHERE table_name="multilinestrings";
DELETE FROM gpkg_contents WHERE table_name="other_relations";
DELETE FROM gpkg_geometry_columns WHERE table_name="other_relations";
DELETE FROM gpkg_extensions WHERE table_name="multilinestrings";
DELETE FROM gpkg_extensions WHERE table_name="other_relations";
DELETE FROM gpkg_geometry_columns WHERE table_name="multilinestrings";
DELETE FROM gpkg_geometry_columns WHERE table_name="other_relations";
"""

INI_TEMPLATE = """
# Configuration file for OSM import

# put here the name of keys for ways that are assumed to be polygons if they are closed
# see http://wiki.openstreetmap.org/wiki/Map_Features
closed_ways_are_polygons=aeroway,amenity,boundary,building,craft,geological,harbour,historic,landuse,leisure,man_made,military,natural,office,place,power,shop,sport,tourism,water,waterway,wetland

# laundering of keys ( ':' turned into '_' )
attribute_name_laundering=no

# uncomment to report all nodes, including the ones without any (significant) tag
#report_all_nodes=yes

# uncomment to report all ways, including the ones without any (significant) tag
#report_all_ways=yes

[points]
# common attributes
osm_id=yes
osm_version=no
osm_timestamp=no
osm_uid=no
osm_user=no
osm_changeset=no

# keys to report as OGR fields
attributes={points_attributes}

# keys that, alone, are not significant enough to report a node as a OGR point
unsignificant=created_by,converted_by,source,time,attribution
# keys that should NOT be reported in the "other_tags" field
ignore=created_by,converted_by,source,time,note,openGeoDB:,fixme,FIXME
# uncomment to avoid creation of "other_tags" field
other_tags=no
# uncomment to create "all_tags" field. "all_tags" and "other_tags" are exclusive
#all_tags=no

[lines]
# common attributes
osm_id=yes
osm_version=no
osm_timestamp=no
osm_uid=no
osm_user=no
osm_changeset=no

# keys to report as OGR fields
attributes={lines_attributes}
# keys that should NOT be reported in the "other_tags" field
ignore=created_by,converted_by,source,time,ele,note,openGeoDB:,fixme,FIXME
# uncomment to avoid creation of "other_tags" field
other_tags=no
# uncomment to create "all_tags" field. "all_tags" and "other_tags" are exclusive
#all_tags=yes

[multipolygons]
# common attributes
# note: for multipolygons, osm_id=yes instanciates a osm_id field for the id of relations
# and a osm_way_id field for the id of closed ways. Both fields are exclusively set.
osm_id=yes
osm_version=no
osm_timestamp=no
osm_uid=no
osm_user=no
osm_changeset=no

# keys to report as OGR fields
attributes={multipolygons_attributes}
# keys that should NOT be reported in the "other_tags" field
ignore=area,created_by,converted_by,source,time,ele,note,openGeoDB:,fixme,FIXME
# uncomment to avoid creation of "other_tags" field
other_tags=no
# uncomment to create "all_tags" field. "all_tags" and "other_tags" are exclusive
#all_tags=yes

[multilinestrings]
# common attributes
osm_id=yes
osm_version=no
osm_timestamp=no
osm_uid=no
osm_user=no
osm_changeset=no

# keys to report as OGR fields
#attributes=access,addr:housename,addr:housenumber,addr:interpolation,admin_level,aerialway,barrier,bridge,boundary,
construction,covered,cutting,denomination,disused,embankment,foot,generator:source,highway,junction,layer,lock,
motorcar,name,natural,oneway,poi,population,railway,ref,religion,route,service,surface,toll,tower:type,tunnel,
waterway,width,wood
# keys that should NOT be reported in the "other_tags" field
ignore=area,created_by,converted_by,source,time,ele,note,openGeoDB:,fixme,FIXME
# uncomment to avoid creation of "other_tags" field
other_tags=no
# uncomment to create "all_tags" field. "all_tags" and "other_tags" are exclusive
#all_tags=yes

[other_relations]
# common attributes
osm_id=yes
osm_version=no
osm_timestamp=no
osm_uid=no
osm_user=no
osm_changeset=no

# keys to report as OGR fields
#attributes=admin_level,aeroway,amenity,boundary,harbour,historic,landuse,leisure,man_made,military,name,natural,
power,place,shop,sport,tourism,type,water,waterway,wetland,unocha:pcode
# keys that should NOT be reported in the "other_tags" field
ignore=area,created_by,converted_by,time,ele,note,openGeoDB:,fixme,FIXME
# uncomment to avoid creation of "other_tags" field
other_tags=no
# uncomment to create "all_tags" field. "all_tags" and "other_tags" are exclusive
#all_tags=yes
"""


class OSMConfig(object):
    """
    Create ogr2ogr OSM conf file based on the template
    at utils/conf/hotosm.ini.tmpl

    See: http://www.gdal.org/drv_osm.html
    """

    def __init__(self, stage_dir, points=[], lines=[], polygons=[], output_filename="osmconf.ini"):
        """
        Initialize the OSMConfig utility.

        Args:
            categories: the export tags categorized by geometry type.
            job_name: the name of the job
        """
        self.points = points
        self.lines = lines
        self.polygons = polygons
        self.output_ini = os.path.join(stage_dir, output_filename)

    def create_osm_conf(self, stage_dir=None):
        """
        Create the osm configuration file.

        Args:
            stage_dir: where to stage the config file.

        Return:
            the path to the export configuration file.
        """
        result = INI_TEMPLATE.format(
            points_attributes=",".join(self.points),
            lines_attributes=",".join(self.lines),
            multipolygons_attributes=",".join(self.polygons),
        )
        with open(self.output_ini, "wb") as f:
            f.write(result.encode())
        return self.output_ini


class Geopackage(object):
    """
    Parse a OSM file (.osm or .pbf) dumped from overpass query.
    Creates an output GeoPackage file to be used in export pipeline.
    """

    name = "geopackage"
    description = "GeoPackage (OSM Schema)"

    def __init__(
        self,
        input_pbf,
        output_gpkg,
        stage_dir,
        feature_selection,
        aoi_geom: Polygon,
        tempdir=None,
        per_theme=False,
        progress=None,
        export_task_record_uid=None,
    ):
        """
        Initialize the OSMParser.

        Args:
            osm: the osm file to convert
            sqlite: the location of the sqlite output file.
        """
        self.path = os.path.dirname(os.path.realpath(__file__))
        self.input_pbf = input_pbf
        self.output_gpkg = output_gpkg
        self.stage_dir = stage_dir
        self.feature_selection = feature_selection
        self.aoi_geom = aoi_geom
        self.per_theme = per_theme
        # Supplying an ExportTaskRecord ID allows progress updates
        self.export_task_record_uid = export_task_record_uid

        """
        OGR Command to run.
        OSM_CONFIG_FILE determines which OSM keys should be translated into OGR layer fields.
        See osmconf.ini for details. See gdal config options at http://www.gdal.org/drv_osm.html
        """
        self.ogr_cmd = Template(
            """
            ogr2ogr -f GPKG $gpkg $osm \
            --config OSM_CONFIG_FILE $osmconf \
            --config OGR_INTERLEAVED_READING YES \
            --config OSM_MAX_TMPFILE_SIZE 100 -gt 65536
        """
        )

        # Enable GDAL/OGR exceptions
        gdal.UseExceptions()
        self.srs = osr.SpatialReference()
        self.srs.ImportFromEPSG(4326)  # configurable
        try:
            os.remove(self.output_gpkg)
        except Exception:
            pass

    def run(self, subtask_percentage=100, subtask_start=0, eta=None):
        """
        Create the GeoPackage from the osm data.
        """
        if self.is_complete:
            logger.debug("Skipping Geopackage, file exists")
            return
        keys_points = self.feature_selection.key_union("points")
        keys_lines = self.feature_selection.key_union("lines")
        keys_polygons = self.feature_selection.key_union("polygons")
        osmconf = OSMConfig(self.stage_dir, points=keys_points, lines=keys_lines, polygons=keys_polygons)
        conf = osmconf.create_osm_conf()
        logger.debug(
            f"Creating OSM gpkg using OSM_MAX_TMPFILE_SIZE {settings.OSM_MAX_TMPFILE_SIZE}"
            f"from {self.input_pbf} to {self.output_gpkg}"
        )
        gdalutils.convert(
            input_file=self.input_pbf,
            output_file=self.output_gpkg,
            driver="GPKG",
            boundary=json.loads(self.aoi_geom.geojson),
            config_options=[
                ("OSM_CONFIG_FILE", conf),
                ("OGR_INTERLEAVED_READING", "YES"),
                ("OSM_MAX_TMPFILE_SIZE", settings.OSM_MAX_TMPFILE_SIZE),
            ],
            task_uid=self.export_task_record_uid,
        )
        # Cancel the provider task if the geopackage has no data.
        if not check_content_exists(self.output_gpkg):
            return None

        """
        Create the default osm gpkg schema
        """
        conn = sqlite3.connect(self.output_gpkg)
        conn.enable_load_extension(True)
        cur = conn.cursor()
        cur.execute("select load_extension('mod_spatialite')")
        cur.execute("CREATE TABLE boundary (id INTEGER PRIMARY KEY AUTOINCREMENT NOT NULL, geom GEOMETRY)")
        cur.execute(
            "INSERT INTO boundary (geom) VALUES (GeomFromWKB(?,4326));",
            (self.aoi_geom.wkb,),
        )

        update_progress(self.export_task_record_uid, 30, subtask_percentage, subtask_start, eta=eta)

        cur.executescript(SPATIAL_SQL)
        self.update_zindexes(cur, self.feature_selection)
        update_progress(self.export_task_record_uid, 42, subtask_percentage, subtask_start, eta=eta)

        # add themes
        create_sqls, index_sqls = self.feature_selection.sqls
        for query in create_sqls:
            logger.debug(query)
            cur.executescript(query)
        update_progress(self.export_task_record_uid, 50, subtask_percentage, subtask_start, eta=eta)

        for query in index_sqls:
            logger.debug(query)
            cur.executescript(query)

        """
        Remove points/lines/multipolygons tables
        """
        cur.execute("DROP TABLE points")
        cur.execute("DROP TABLE lines")
        cur.execute("DROP TABLE multipolygons")

        cur.execute("VACUUM;")

        conn.commit()
        conn.close()

        if self.per_theme:
            # this creates per-theme GPKGs
            for theme in self.feature_selection.themes:
                conn = sqlite3.connect(self.stage_dir + slugify(theme) + ".gpkg")
                conn.enable_load_extension(True)
                cur = conn.cursor()
                cur.execute("attach database ? as 'geopackage'", (self.output_gpkg,))
                cur.execute("create table gpkg_spatial_ref_sys as select * from geopackage.gpkg_spatial_ref_sys")
                cur.execute("create table gpkg_contents as select * from geopackage.gpkg_contents where 0")
                cur.execute(
                    "create table gpkg_geometry_columns as select * from geopackage.gpkg_geometry_columns where 0"
                )
                for geom_type in self.feature_selection.geom_types(theme):
                    for stmt in self.feature_selection.create_sql(theme, geom_type):
                        cur.executescript(stmt)
                cur.execute("VACUUM;")
                conn.commit()
                conn.close()

        update_progress(self.export_task_record_uid, 100, subtask_percentage, subtask_start, eta=eta)
        return self.output_gpkg

    @property
    def is_complete(self):
        return os.path.isfile(self.output_gpkg)

    @property
    def results(self):
        if self.per_theme:
            results_list = []
            for theme in self.feature_selection.themes:
                results_list.append(
                    Artifact(
                        [os.path.join(self.stage_dir, slugify(theme)) + ".gpkg"],
                        Geopackage.name,
                        theme=theme,
                    )
                )
            return results_list
        else:
            return [Artifact([self.output_gpkg], Geopackage.name)]

    def update_zindexes(self, cur, feature_selection):
        # arguably, determing Z-index should require all 5 of these OSM keys
        # to construct a consistent z-index.
        for geom_type in ["point", "line", "polygon"]:
            key_union = feature_selection.key_union(geom_type + "s")  # boo
            MAPPING = {"point": "points", "line": "lines", "polygon": "multipolygons"}
            table_name = MAPPING[geom_type]
            if any([x in key_union for x in ["highway", "railway", "layer", "bridge", "tunnel"]]):
                cur.execute("ALTER TABLE {table} ADD COLUMN z_index SMALLINT DEFAULT 0;".format(table=table_name))
                if "highway" in key_union:
                    cur.executescript(
                        """
                        UPDATE {table} SET z_index = 3 WHERE highway IN ('path', 'track', 'footway', 'minor',
                        'road', 'service', 'unclassified', 'residential');
                        UPDATE {table} SET z_index = 4 WHERE highway IN ('tertiary_link', 'tertiary');
                        UPDATE {table} SET z_index = 6 WHERE highway IN ('secondary_link', 'secondary');
                        UPDATE {table} SET z_index = 7 WHERE highway IN ('primary_link', 'primary');
                        UPDATE {table} SET z_index = 8 WHERE highway IN  ('trunk_link', 'trunk');
                        UPDATE {table} SET z_index = 9 WHERE highway IN  ('motorway_link', 'motorway');
                    """.format(
                            table=table_name
                        )
                    )
                if "railway" in key_union:
                    cur.execute(
                        "UPDATE {table} SET z_index = z_index + 5 WHERE railway IS NOT NULL".format(table=table_name)
                    )
                if "layer" in key_union:
                    cur.execute(
                        "UPDATE {table} SET z_index = z_index + 10 * cast(layer AS SMALLINT) WHERE layer IS NOT NULL".format(  # NOQA
                            table=table_name
                        )
                    )
                if "bridge" in key_union:
                    cur.execute(
                        "UPDATE {table} SET z_index = z_index + 10 WHERE bridge IN ('yes', 'true', 1)".format(
                            table=table_name
                        )
                    )
                if "tunnel" in key_union:
                    cur.execute(
                        "UPDATE {table} SET z_index = z_index - 10 WHERE tunnel IN ('yes', 'true', 1)".format(
                            table=table_name
                        )
                    )


def add_geojson_to_geopackage(geojson=None, gpkg=None, layer_name=None, task_uid=None, user_details=None):
    """Uses an ogr2ogr script to upload a geojson file.
    Args:
        geojson: A geojson string.
        gpkg: Database dict from the django settings.
        layer_name: A DB table.
        task_uid: A task uid to update.
    Returns:
        True if the file is successfully uploaded.
    """
    # This is just to make it easier to trace when user_details haven't been sent
    if user_details is None:
        user_details = {"username": "unknown-add_geojson_to_geopackage"}

    if not geojson or not gpkg:
        raise Exception(
            "A valid geojson: {0} was not provided\nor a geopackage: {1} was not accessible.".format(geojson, gpkg)
        )

    geojson_file = os.path.join(
        os.path.dirname(gpkg),
        "{0}.geojson".format(os.path.splitext(os.path.basename(gpkg))[0]),
    )

    from audit_logging.file_logging import logging_open

    with logging_open(geojson_file, "w", user_details=user_details) as open_file:
        open_file.write(geojson)

    gpkg = gdalutils.convert(
        driver="gpkg",
        input_file=gpkg,
        output_file=geojson_file,
        task_uid=task_uid,
        creation_options=f"-nln {layer_name}",
    )

    return gpkg


def is_alnum(data):
    """
    Used to ensure that only 'safe' data can be used to query or create data.
    >>> is_alnum("test")
    True
    >>> is_alnum("test_2")
    True
    >>> is_alnum(";")
    False
    >>> is_alnum("test 4")
    False
    @param: String of data to be tested.
    @return: if data is only alphanumeric or '_' chars.
    """
    import re

    if re.match(r"[\w:]+$", data):
        return True
    return False


def get_table_count(gpkg, table):
    """
    :param gpkg: Path to geopackage file.
    :param table: A table name to count the rows.
    :return: A count of the rows in a table.
    """
    with sqlite3.connect(gpkg) as conn:
        if is_alnum(table):
            result = conn.execute("SELECT COUNT(*) FROM '{0}';".format(table))
            return result.fetchone()[0]
    return False


def get_table_names(gpkg):
    """
    Gets the list of the feature or tile data table names

    :param gpkg: Path to geopackage file.
    :return: List of user data table names in geopackage.
    """
    with sqlite3.connect(gpkg) as conn:
        result = conn.execute("SELECT table_name FROM gpkg_contents;")
    return [table for (table,) in result]


def get_tile_table_names(gpkg):
    """
    Gets the list of tile table names.

    :param gpkg: Path to geopackage file.
    :return: List of tile user data table names in geopackage.
    """
    with sqlite3.connect(gpkg) as conn:
        result = conn.execute("SELECT table_name FROM gpkg_contents WHERE data_type = 'tiles';")
        return [table for (table,) in result]


def get_table_gpkg_contents_information(gpkg, table_name):
    """

    :param gpkg: Path to geopackage file.
    :param table_name: A table name to look up in gpkg_contents.
    :return: A dict with the column names as the keys.
    """
    with sqlite3.connect(gpkg) as conn:
        result = conn.execute(
            "SELECT table_name, data_type, identifier, description, last_change, min_x, min_y, max_x, max_y, srs_id "
            "FROM gpkg_contents WHERE table_name = '{0}';".format(table_name)
        )
        table_information = result.fetchone()
        return {
            "table_name": table_information[0],
            "data_type": table_information[1],
            "identifier": table_information[2],
            "description": table_information[3],
            "last_change": table_information[4],
            "min_x": table_information[5],
            "min_y": table_information[6],
            "max_x": table_information[7],
            "max_y": table_information[8],
            "srs_id": table_information[9],
        }


def set_gpkg_contents_bounds(gpkg, table_name, bbox):
    """

    :param gpkg: Path to geopackage file.
    :param table_name: A table name to set the bounds.
    :param bbox: An iterable with doubles representing the bounds [w,s,e,n]
    :return: A dict with the column names as the key.
    """
    with sqlite3.connect(gpkg) as conn:
        if not conn.execute(
            "UPDATE gpkg_contents SET min_x = {0}, min_y = {1}, max_x = {2}, max_y = {3} WHERE table_name = '{4}';".format(  # NOQA
                bbox[0], bbox[1], bbox[2], bbox[3], table_name
            )
        ).rowcount:
            logger.error("Unable to set bounds for the table `{0}` in {1}".format(table_name, gpkg))
            raise Exception("Unable to set the bounds in the geopackage.")


def get_table_tile_matrix_information(gpkg, table_name):
    with sqlite3.connect(gpkg) as conn:
        result = conn.execute(
            "SELECT table_name, zoom_level, matrix_width, matrix_height, tile_width, tile_height, pixel_x_size, "
            "pixel_y_size FROM gpkg_tile_matrix WHERE table_name = '{0}' ORDER BY zoom_level;".format(table_name)
        )
        tile_matrix_information = []
        for table_information in result:
            tile_matrix_information += [
                {
                    "table_name": table_information[0],
                    "zoom_level": table_information[1],
                    "matrix_width": table_information[2],
                    "matrix_height": table_information[3],
                    "tile_width": table_information[4],
                    "tile_height": table_information[5],
                    "pixel_x_size": table_information[6],
                    "pixel_y_size": table_information[7],
                }
            ]
    return tile_matrix_information


def get_zoom_levels_table(gpkg, table):
    """
    Inspects the tile user data table for unique zoom levels.
    :param gpkg: Path to geopackage
    :param table: A table name to return the zoom_levels which have data in the user table.
    :return: A list of zoom levels.
    """
    with sqlite3.connect(gpkg) as conn:
        if is_alnum(table):
            result = conn.execute("SELECT DISTINCT zoom_level FROM '{0}';".format(table))
            return [zoom_level for (zoom_level,) in result]
    return False


def remove_empty_zoom_levels(gpkg):
    """
    Inspects geopackage for tile tables, and ensures that the tile matrix lists only levels with data in it.
    :param gpkg: Path to geopackage
    :return: None
    """
    for table in get_tile_table_names(gpkg):
        populated_zoom_levels = get_zoom_levels_table(gpkg, table)
        for zoom_level in get_tile_matrix_table_zoom_levels(gpkg, table):
            if zoom_level not in populated_zoom_levels:
                remove_zoom_level(gpkg, table, zoom_level)


def remove_zoom_level(gpkg, table, zoom_level):
    """
    Removes a specific zoom level, for a table in the gpkg_tile_matrix table.

    :param gpkg: Path to geopackage.
    :param table: Table name in gpkg_tile_matrix.
    :param zoom_level: A specific zoom level to remove from gpkg_tile_matrix.
    :return:
    """
    with sqlite3.connect(gpkg) as conn:
        if is_alnum(table):
            if conn.execute(
                "DELETE FROM gpkg_tile_matrix "
                "WHERE table_name = '{0}' AND zoom_level = '{1}';".format(table, zoom_level)
            ).rowcount:
                return True
        raise Exception("Unable to remove zoom level {0} for {1} from {2}".format(zoom_level, table, gpkg))


def get_tile_matrix_table_zoom_levels(gpkg, table_name):
    """
    Returns the zoom levels listed in the gpkg_tile_matrix for a specific table_name.

    :param gpkg: Path to geopackage file.
    :param table_name: Table to query zoom_levels for in the gpkg_tile_matrix.
    :return: List of zoom levels (i.e. [2,3,4,5]
    """
    with sqlite3.connect(gpkg) as conn:
        if is_alnum(table_name):
            result = conn.execute(
                "SELECT zoom_level " "FROM gpkg_tile_matrix WHERE table_name = '{0}';".format(table_name)
            )
            return [zoom_level for (zoom_level,) in result]
    return False


def check_content_exists(gpkg):
    """
    :param gpkg: Path to geopackage file.
    :return: True if there is a single raster tile or feature is found.
    """
    for table in get_table_names(gpkg):
        if get_table_count(gpkg, table) > 0:
            return True
    return False


def check_zoom_levels(gpkg):
    """
    Checks the zoom levels for the geopackage returns False if ANY gpkg_tile_matrix sets do no match the zoom levels
    of the user data tables.

    :param gpkg: Path to geopackage file.
    :return: True if the zoom levels in the data tables match the zoom levels in the gpkg_tile_matrix_table
    """
    for table in get_table_names(gpkg):
        if not get_tile_matrix_table_zoom_levels(gpkg, table) == get_zoom_levels_table(gpkg, table):
            return False
    return True


def get_table_info(gpkg, table):
    """
    Checks the zoom levels for the geopackage returns False if ANY gpkg_tile_matrix sets do no match the zoom levels
    of the user data tables.

    :param gpkg: Path to geopackage file.
    :return: The type of the first value in  if the zoom levels in the data tables match the zoom levels in the
    gpkg_tile_matrix_table
    """
    with sqlite3.connect(gpkg) as conn:
        logger.debug("PRAGMA table_info({0});".format(table))
        return conn.execute("PRAGMA table_info({0});".format(table))
    return False


def create_table_from_existing(gpkg, old_table, new_table):
    """
    Creates a new gpkg table, from an existing table.  This assumed the original table is from a gpkg and as
    such has a primary key column.

    :param gpkg:
    :param old_table:
    :param new_table:
    :return:
    """
    columns = [("id", "INTEGER PRIMARY KEY AUTOINCREMENT")]
    for (cid, name, type, notnull, dflt_value, pk) in get_table_info(gpkg, old_table):
        if pk:
            columns = [(name, "INTEGER PRIMARY KEY AUTOINCREMENT")]
        else:
            columns += [(name, type)]
    with sqlite3.connect(gpkg) as conn:
        logger.debug(
            "CREATE TABLE {0} ({1});".format(
                new_table,
                ",".join(["{0} {1}".format(column[0], column[1]) for column in columns]),
            )
        )
        conn.execute(
            "CREATE TABLE {0} ({1});".format(
                new_table,
                ",".join(["{0} {1}".format(column[0], column[1]) for column in columns]),
            )
        )


def create_metadata_tables(gpkg):
    """
    Creates tables needed to add metadata.

    :param gpkg: A geopackage to create the metadata tables.
    :return:
    """
    create_extension_table(gpkg)
    commands = [
        """
        CREATE TABLE IF NOT EXISTS gpkg_metadata (
          id INTEGER CONSTRAINT m_pk PRIMARY KEY ASC NOT NULL,
          md_scope TEXT NOT NULL DEFAULT 'dataset',
          md_standard_uri TEXT NOT NULL,
          mime_type TEXT NOT NULL DEFAULT 'text/xml',
          metadata TEXT NOT NULL DEFAULT ''
        );
        """,
        """
        CREATE TABLE IF NOT EXISTS gpkg_metadata_reference (
          reference_scope TEXT NOT NULL,
          table_name TEXT,
          column_name TEXT,
          row_id_value INTEGER,
          timestamp DATETIME NOT NULL DEFAULT (strftime('%Y-%m-%dT%H:%M:%fZ','now')),
          md_file_id INTEGER NOT NULL,
          md_parent_id INTEGER,
          CONSTRAINT crmr_mfi_fk FOREIGN KEY (md_file_id) REFERENCES gpkg_metadata(id),
          CONSTRAINT crmr_mpi_fk FOREIGN KEY (md_parent_id) REFERENCES gpkg_metadata(id)
        );
        """,
        """
        INSERT OR IGNORE INTO gpkg_extensions(table_name, column_name, extension_name, definition, scope)
            VALUES (NULL, NULL, "gpkg_metadata", "http://www.geopackage.org/spec/#extension_metadata", "read-write");
        """,
    ]
    with sqlite3.connect(gpkg) as conn:
        for command in commands:
            logger.debug(command)
            conn.execute(command)


def create_extension_table(gpkg):
    """

    :param gpkg: A geopackage to create the gpkg_extensions table.
    :return:
    """
    command = """
CREATE TABLE IF NOT EXISTS gpkg_extensions (
  table_name TEXT,
  column_name TEXT,
  extension_name TEXT NOT NULL,
  definition TEXT NOT NULL,
  scope TEXT NOT NULL,
  CONSTRAINT ge_tce UNIQUE (table_name, column_name, extension_name)
);
"""

    with sqlite3.connect(gpkg) as conn:
        logger.debug(command)
        conn.execute(command)


def add_file_metadata(gpkg, metadata):
    """
    :param gpkg: A geopackage to add metadata.
    :param metadata: The xml metadata to add as a string.
    :return:
    """
    create_metadata_tables(gpkg)

    with sqlite3.connect(gpkg) as conn:
        command = (
            "INSERT OR IGNORE INTO gpkg_metadata (md_scope, md_standard_uri, mime_type, metadata)"
            "VALUES ('dataset', 'http://schemas.opengis.net/iso/19139/20070417/resources/Codelist/"
            "gmxCodelists.xml#MD_ScopeCode', 'text/xml', ?);"
        )
        logger.debug(command)
        conn.execute(command, (metadata,))

        command = """
INSERT OR IGNORE INTO gpkg_metadata_reference (reference_scope, table_name, column_name, row_id_value,
 timestamp, md_file_id, md_parent_id)
VALUES ('geopackage', NULL, NULL, NULL, strftime('%Y-%m-%dT%H:%M:%fZ','now'), 1, null);
                 """
        logger.debug(command)
        conn.execute(command)
