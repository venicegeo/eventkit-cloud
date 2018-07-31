# -*- coding: utf-8 -*-
from __future__ import absolute_import

import unittest

from eventkit_cloud.feature_selection.feature_selection import FeatureSelection

ZIP_README = """
This thematic file was generated by EventKit.

This theme includes features matching the filter:

column2 IS NOT NULL

clipped to the area defined by the included boundary.geojson.

This theme includes the following OpenStreetMap keys:

column1 http://wiki.openstreetmap.org/wiki/Key:column1

(c) OpenStreetMap contributors.

This file is made available under the Open Database License: http://opendatacommons.org/licenses/odbl/1.0/. Any rights in individual contents of the database are licensed under the Database Contents License: http://opendatacommons.org/licenses/dbcl/1.0/
"""

class TestFeatureSelection(unittest.TestCase):
    maxDiff = None

    def test_reserved_table_names(self):
        # these table names are used by the ogr2ogr gpkg importer
        y = '''
        points:
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Theme name reserved: points")
        y = '''
        rtree_something:
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Theme name reserved: rtree_something")
        y = '''
        gpkg_something:
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Theme name reserved: gpkg_something")

    def test_empty_feature_selection(self):
        y = '''
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)

    def test_theme_names(self):
        y = '''
        A Theme Name:
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertTrue(f.valid)
        self.assertEqual(f.themes,["A Theme Name"])
        self.assertEqual(f.slug_themes,["a_theme_name"])


    def test_key_union_and_filters(self):
        y = '''
        waterways:
            types: 
                - lines
                - polygons
            select:
                - name
                - waterway
        buildings:
            types:
                - points
                - lines
                - polygons
            select:
                - name
                - building
            where: building IS NOT NULL
        '''
        f = FeatureSelection(y)
        self.assertEquals(f.themes,['buildings','waterways'])
        self.assertEquals(f.geom_types('waterways'),['lines','polygons'])
        self.assertEquals(f.key_selections('waterways'),['name','waterway'])
        self.assertEquals(f.filter_clause('waterways'),'"name" IS NOT NULL OR "waterway" IS NOT NULL')
        self.assertEquals(f.key_union(), ['building','name','waterway'])
        self.assertEquals(f.key_union('points'), ['building','name'])
        self.assertEquals(f.filter_clause('buildings'),'building IS NOT NULL')

    def test_sql_list(self):
        y = '''
        waterways:
            types:
                - polygons
            select:
                - name
            where:
                - name IS NOT NULL
                - name = 'some building'
        '''
        f = FeatureSelection(y)
        self.assertEquals(f.filter_clause('waterways'),"name IS NOT NULL OR name = 'some building'")

    def test_sqls(self):
        y = '''
        buildings:
            types:
                - points
                - polygons
            select:
                - name
                - addr:housenumber
        '''
        f = FeatureSelection(y)
        create_sqls, index_sqls = f.sqls
        self.assertEquals(create_sqls[0],'CREATE TABLE buildings_points(\nfid INTEGER PRIMARY KEY AUTOINCREMENT,\ngeom POINT,\nosm_id TEXT,"name" TEXT,"addr:housenumber" TEXT\n);\nINSERT INTO buildings_points(geom, osm_id,"name","addr:housenumber") select geom, osm_id,"name","addr:housenumber" from points WHERE ("name" IS NOT NULL OR "addr:housenumber" IS NOT NULL);\n')
        self.assertEquals(create_sqls[1],'CREATE TABLE buildings_polygons(\nfid INTEGER PRIMARY KEY AUTOINCREMENT,\ngeom MULTIPOLYGON,\nosm_id TEXT,osm_way_id TEXT,"name" TEXT,"addr:housenumber" TEXT\n);\nINSERT INTO buildings_polygons(geom, osm_id,osm_way_id,"name","addr:housenumber") select geom, osm_id,osm_way_id,"name","addr:housenumber" from multipolygons WHERE ("name" IS NOT NULL OR "addr:housenumber" IS NOT NULL);\n')

    def test_zindex(self):
        y = '''
        roads:
            types:
                - lines 
            select:
                - highway
        '''
        f = FeatureSelection(y)
        create_sqls, index_sqls = f.sqls
        self.assertEquals(create_sqls[0],'CREATE TABLE roads_lines(\nfid INTEGER PRIMARY KEY AUTOINCREMENT,\ngeom MULTILINESTRING,\nosm_id TEXT,"highway" TEXT,"z_index" TEXT\n);\nINSERT INTO roads_lines(geom, osm_id,"highway","z_index") select geom, osm_id,"highway","z_index" from lines WHERE ("highway" IS NOT NULL);\n')


    def test_unsafe_yaml(self):
        y = '''
        !!python/object:feature_selection.feature_selection.FeatureSelection
        a: 0
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(1,len(f.errors))

    def test_malformed_yaml(self):
        # if it's not a valid YAML document
        # TODO: errors for if yaml indentation is incorrect
        y = '''
        all
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)

    def test_minimal_yaml(self):
        # the shortest valid feature selection
        y = '''
        all: 
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertTrue(f.valid)
        self.assertEqual(f.geom_types('all'),['points','lines','polygons'])

    def test_unspecified_yaml(self):
        # top level is a list and not a dict
        y = '''
        - all: 
            select:
                - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"YAML must be dict, not list")

    def test_dash_spacing_yaml(self):
        # top level is a list and not a dict
        y = '''
        all: 
          select:
            -name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)

    def test_no_select_yaml(self):
        # top level is a list and not a dict
        y = '''
        all: 
          -select:
            - name
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Each theme must have a 'select' key")

    # refer to https://taginfo.openstreetmap.org/keys
    def test_valid_invalid_key_yaml(self):
        y = '''
        all: 
          select:
            - has space
            - has_underscore
            - has:colon
            - UPPERCASE
        '''
        f = FeatureSelection(y)
        self.assertTrue(f.valid)
        y = '''
        all: 
          select:
            - na?me
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Invalid OSM key: na?me")
        y = '''
        all: 
          select:
            -
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEqual(f.errors[0],"Missing OSM key")

    def test_passes_sqlvalidator_errors(self):
        y = '''
        buildings:
            select:
                - name
                - addr:housenumber
            where: addr:housenumber IS NOT NULL
        '''
        f = FeatureSelection(y)
        self.assertFalse(f.valid)
        self.assertEquals(f.errors[0], "SQL WHERE Invalid: identifier with colon : must be in double quotes.")

    def test_enforces_subset_columns(self):
        y = '''
        buildings:
            types:
                - polygons
            select:
                - column1 
            where: column2 IS NOT NULL
        other:
            types:
                - points
            select:
                - column3
        '''
        f = FeatureSelection(y)
        self.assertTrue(f.valid)
        self.assertEquals(f.key_union(), ['column1','column2','column3'])
        self.assertEquals(f.key_union('points'), ['column3'])

    def test_zip_readme(self):
        y = '''
        buildings:
            select:
                - column1 
            where: column2 IS NOT NULL
        other:
            select:
                - column3
        '''
        f = FeatureSelection(y)
        self.assertMultiLineEqual(f.zip_readme('buildings'),ZIP_README)
