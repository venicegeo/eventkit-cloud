import ol from 'openlayers';
import Reader from 'jsts/org/locationtech/jts/io/GeoJSONReader';
import isEqual from 'lodash/isEqual';
import toString from 'lodash/toString';
import GeoJSONWriter from 'jsts/org/locationtech/jts/io/GeoJSONWriter';
import BufferOp from 'jsts/org/locationtech/jts/operation/buffer/BufferOp';
import UnionOp from 'jsts/org/locationtech/jts/operation/union/UnionOp';
import isValidOp from 'jsts/org/locationtech/jts/operation/valid/IsValidOp';
import BufferParameters from 'jsts/org/locationtech/jts/operation/buffer/BufferParameters';

export const MODE_DRAW_BBOX = 'MODE_DRAW_BBOX';
export const MODE_NORMAL = 'MODE_NORMAL';
export const MODE_DRAW_FREE = 'MODE_DRAW_FREE';

export const WGS84 = 'EPSG:4326';
export const WEB_MERCATOR = 'EPSG:3857';

/**
 * Convert a jsts geometry to an openlayers3 geometry
 * @param {jstsGeom} a JSTS geometry in EPSG:3857
 * @return {olGeom} an openlayers3 geometry in EPSG:4326
 */
export function jstsGeomToOlGeom(jstsGeom) {
    const writer = new GeoJSONWriter();
    const olReader = new ol.format.GeoJSON();
    const olGeom = olReader.readGeometry(writer.write(jstsGeom)).transform('EPSG:4326', 'EPSG:3857');
    return olGeom;
}

/**
 * Converts a JSTS Polygon/MultiPolygon geometry from one reference system to another
 * @param {jstsGeometry} A JSTS geometry.
 * @param {from_srs} An EPSG code as a string, example: "EPSG:4326".
 * @param {to_srs} An EPSG code as a string, example: "EPSG:3857".
 * @return {jstsGeometry} A JSTS geometry.
 */
export function transformJSTSGeometry(jstsGeometry, fromSrs, toSrs) {
    // This all seems excessive, however jsts ol3Parser wasn't working with versions
    // "jsts": "~1.4.0" and "openlayers": "~3.19.1", worth revisting in the future.
    const writer = new GeoJSONWriter();
    const geojsonReader = new Reader();
    const ol3GeoJSON = new ol.format.GeoJSON();
    const geom = (new ol.format.GeoJSON())
        .readGeometry(writer.write(jstsGeometry)).transform(fromSrs, toSrs);
    return geojsonReader.read(ol3GeoJSON.writeGeometry(geom));
}


/**
 * Creates a buffer around a jsts geometry if not a Polygon or MultiPolygon.
 * @param {jstsGeometry} A JSTS geometry.
 * @param {bufferSize} The size of the buffer (in meteres)
 * @param {bufferPolys} Whether polygon/multipolygon features should be buffered
 * @return {jstsGeometry} A JSTS geometry.
 */
export function bufferGeometry(jstsGeometry, bufferSize, bufferPolys) {
    // This buffers jsts points so that those features will have an actual area to be collected.
    // The buffer size is relative to the unit of measurement for the projection.
    // In order to get meters and circles, 3857 should be used.
    const bufferParams = new BufferParameters();
    bufferParams.setJoinStyle(2);

    const size = bufferSize || 1;
    if (bufferPolys) {
        const tempGeom = transformJSTSGeometry(jstsGeometry, 'EPSG:4326', 'EPSG:3857');
        const buff = new BufferOp(tempGeom, bufferParams);
        const geomBuff = buff.getResultGeometry(size);
        return transformJSTSGeometry(geomBuff, 'EPSG:3857', 'EPSG:4326');
    } else if (!(jstsGeometry.getGeometryType() === 'Polygon' || jstsGeometry.getGeometryType() === 'MultiPolygon')) {
        const tempGeom = transformJSTSGeometry(jstsGeometry, 'EPSG:4326', 'EPSG:3857');
        return transformJSTSGeometry(BufferOp.bufferOp(tempGeom, size), 'EPSG:3857', 'EPSG:4326');
    }
    return jstsGeometry;
}

/**
 * Converts a GeoJSON to a JSTS Polygon/MultiPolygon geometry
 * @param {geojson} A geojson object.
 * @param {bufferSize} The size of the buffer (in meters)
 * @param {bufferPolys} Whether polygon/multipolygon features should be buffered
 * @return {geometry} A JSTS Polygon or MultiPolygon
 */
export function convertGeoJSONtoJSTS(geojson, bufferSize, bufferPolys) {
    const geojsonReader = new Reader();

    const jstsGeoJSON = geojsonReader.read(geojson);

    let geometry;
    if (jstsGeoJSON.features) {
        const { features } = jstsGeoJSON;
        geometry = bufferGeometry(features[0].geometry, bufferSize, bufferPolys);
        for (let i = 1; i < features.length; i += 1) {
            geometry = UnionOp.union(
                geometry,
                bufferGeometry(features[i].geometry, bufferSize, bufferPolys),
            );
        }
    } else if (jstsGeoJSON.geometries) {
        const { geometries } = jstsGeoJSON;
        geometry = bufferGeometry(geometries[0], bufferSize, bufferPolys);
        for (let i = 1; i < geometries.length; i += 1) {
            geometry = UnionOp.union(
                geometry,
                bufferGeometry(geometries[i], bufferSize, bufferPolys),
            );
        }
    } else if (jstsGeoJSON.geometry) {
        geometry = bufferGeometry(jstsGeoJSON.geometry, bufferSize, bufferPolys);
    } else {
        geometry = bufferGeometry(jstsGeoJSON, bufferSize, bufferPolys);
    }
    return geometry;
}

export function zoomToExtent(optOption) {
    const options = optOption || {};
    options.className = options.className !== undefined ? options.className : '';

    const button = document.createElement('button');
    const icon = document.createElement('i');
    icon.className = 'fa fa-globe';
    button.appendChild(icon);
    let this_ = this;

    this.zoomer = () => {
        const map = this_.getMap();
        const view = map.getView();
        const size = map.getSize();
        const extent = !options.extent ? view.getProjection().getExtent() : options.extent;
        view.fit(extent, size);
    };

    button.addEventListener('click', this_.zoomer, false);
    button.addEventListener('touchstart', this_.zoomer, false);
    const element = document.createElement('div');
    element.className = `${options.className} ol-unselectable ol-control`;
    element.appendChild(button);

    ol.control.Control.call(this, {
        element,
        target: options.target,
    });
}

export function generateDrawLayer() {
    return new ol.layer.Vector({
        source: new ol.source.Vector({
            wrapX: true,
        }),
        style: new ol.style.Style({
            stroke: new ol.style.Stroke({
                color: '#ce4427',
                width: 3,
            }),
            image: new ol.style.Icon({
                src: require("../../images/ic_room_black_24px.svg"),
            }),

        }),
    });
}

export function generateDrawBoxInteraction(drawLayer) {
    const draw = new ol.interaction.Draw({
        source: drawLayer.getSource(),
        type: 'Circle',
        wrapX: true,
        geometryFunction: ol.interaction.Draw.createBox(),
        freehand: true,
        style: new ol.style.Style({
            image: new ol.style.RegularShape({
                stroke: new ol.style.Stroke({
                    color: 'black',
                    width: 1,
                }),
                points: 4,
                radius: 15,
                radius2: 0,
                angle: 0,
            }),
            stroke: new ol.style.Stroke({
                color: '#ce4427',
                width: 2,
                lineDash: [5, 5],
            }),
        }),
    });
    draw.setActive(false);
    return draw;
}

export function generateDrawFreeInteraction(drawLayer) {
    const draw = new ol.interaction.Draw({
        source: drawLayer.getSource(),
        type: 'Polygon',
        wrapX: true,
        freehand: false,
        style: new ol.style.Style({
            image: new ol.style.RegularShape({
                stroke: new ol.style.Stroke({
                    color: 'black',
                    width: 1,
                }),
                points: 4,
                radius: 15,
                radius2: 0,
                angle: 0,
            }),
            stroke: new ol.style.Stroke({
                color: '#ce4427',
                width: 2,
                lineDash: [5, 5],
            }),
        }),
    });
    draw.setActive(false);
    return draw;
}

export function truncate(number) {
    return Math.round(number * 100000) / 100000;
}

export function unwrapPoint([x, y]) {
    return [
        x > 0 ? Math.min(180, x) : Math.max(-180, x),
        y,
    ];
}

export function featureToBbox(feature) {
    const reader = new ol.format.GeoJSON();
    const geometry = reader.readGeometry(feature.geometry, { featureProjection: WEB_MERCATOR });
    return geometry.getExtent();
}

export function deserialize(serialized) {
    if (serialized && serialized.length === 4) {
        return ol.proj.transformExtent(serialized, WGS84, WEB_MERCATOR);
    }
    return null;
}

export function serialize(extent) {
    const bbox = ol.proj.transformExtent(extent, WEB_MERCATOR, WGS84);
    const p1 = unwrapPoint(bbox.slice(0, 2));
    const p2 = unwrapPoint(bbox.slice(2, 4));
    return p1.concat(p2).map(truncate);
}

export function isGeoJSONValid(geojson) {
    // creates a jsts GeoJSONReader
    const parser = new Reader();
    // reads in geojson geometry and returns a jsts geometry
    const geom = parser.read(geojson.features[0].geometry);
    // return whether the geom is valid
    return isValidOp.isValid(geom);
}

export function createGeoJSONGeometry(ol3Geometry) {
    const geom = ol3Geometry.clone();
    geom.transform(WEB_MERCATOR, WGS84);
    const coords = geom.getCoordinates();
    const geojsonGeom = {
        type: geom.getType(),
        coordinates: coords,
    };
    return geojsonGeom;
}

export function createGeoJSON(ol3Geometry) {
    const bbox = serialize(ol3Geometry.getExtent());
    const geojson = {
        type: 'FeatureCollection',
        features: [
            {
                type: 'Feature',
                bbox,
                geometry: createGeoJSONGeometry(ol3Geometry),
            },
        ],
    };
    return geojson;
}

export function clearDraw(drawLayer) {
    drawLayer.getSource().clear();
}

export function zoomToGeometry(geom, map) {
    if (geom.getType() !== 'Point') {
        map.getView().fit(geom);
    } else {
        map.getView().setCenter(geom.getCoordinates());
    }
}

export function featureToPoint(feature) {
    if (!feature) { return null; }
    const center = ol.extent.getCenter(feature.getGeometry().getExtent());
    return new ol.geom.Point(center);
}

// if any coordinates are wrapped adjust them to be within the projection extent
export function unwrapCoordinates(coords, projection) {
    // based on:
    // https://github.com/openlayers/openlayers/blob/2bff72122757b8eeb3f3dda6191d1301ff296948/src/ol/renderer/maprenderer.js#L155
    const projectionExtent = projection.getExtent();
    const worldWidth = ol.extent.getWidth(projectionExtent);
    return coords.map(coord => (
        coord.map((xy) => {
            const x = xy[0];
            if (x < projectionExtent[0] || x > projectionExtent[2]) {
                const worldsAway = Math.ceil((projectionExtent[0] - x) / worldWidth);
                return [x + worldWidth * worldsAway, xy[1]];
            }
            return xy;
        })
    ));
}

export function unwrapExtent(extent, projection) {
    const projectionExtent = projection.getExtent();
    const worldWidth = ol.extent.getWidth(projectionExtent);
    let minX = extent[0];
    if (minX < projectionExtent[0] || minX > projectionExtent[2]) {
        const worldsAway = Math.ceil((projectionExtent[0] - minX) / worldWidth);
        minX = minX + worldWidth * worldsAway;
    }
    let maxX = extent[2];
    if (maxX < projectionExtent[0] || maxX > projectionExtent[2]) {
        const worldsAway = Math.ceil((projectionExtent[0] - maxX) / worldWidth);
        maxX = maxX + worldWidth * worldsAway;
    }
    return [minX, extent[1], maxX, extent[3]];
}

// check if the view center is outside of the 'valid' extent
export function isViewOutsideValidExtent(view) {
    const projectionExtent = view.getProjection().getExtent();
    const center = view.getCenter();
    return center[0] < projectionExtent[0] || center[0] > projectionExtent[2];
}

// calculate what the 'valid' view center should be and set it
export function goToValidExtent(view) {
    const projectionExtent = view.getProjection().getExtent();
    const worldWidth = ol.extent.getWidth(projectionExtent);
    const center = view.getCenter();
    const worldsAway = Math.ceil((projectionExtent[0] - center[0]) / worldWidth);
    view.setCenter([center[0] + worldWidth * worldsAway, center[1]]);
    return view.getCenter();
}

// check if a polygon has the shape of a rectangle
export function isBox(feature) {
    let featCoords = feature.getGeometry().getCoordinates();
    // if there are more than 5 coordinate pairs it can not be a box
    if (featCoords[0].length !== 5) {
        return false;
    }
    // if the extent geometry is the same as the feature geometry we know it is a box
    const extent = feature.getGeometry().getExtent();
    const extentGeom = ol.geom.Polygon.fromExtent(extent);
    let extentCoords = extentGeom.getCoordinates();

    // since the 5th coord is the same as the first remove it,
    // duplicate messes with the comparison if coordinates are in a different order
    // there is probably a better way to compare arrays with different order of sub arrays,
    // but this is what ive got for now
    featCoords = [
        toString(featCoords[0][0]),
        toString(featCoords[0][1]),
        toString(featCoords[0][2]),
        toString(featCoords[0][3]),
    ].sort();
    extentCoords = [
        toString(extentCoords[0][0]),
        toString(extentCoords[0][1]),
        toString(extentCoords[0][2]),
        toString(extentCoords[0][3]),
    ].sort();

    return isEqual(featCoords, extentCoords);
}

// check if the pixel in question lies over a feature vertex, if it does, return the vertex coords
export function isVertex(pixel, feature, tolarance, map) {
    // check target pixel with pixel for each corner of the box, 
    // if within tolerance or equal, we have a vertex
    tolarance = tolarance || 3;
    const geomType = feature.getGeometry().getType();
    let coords = feature.getGeometry().getCoordinates();
    coords = geomType == 'Point' ? [coords] : geomType == 'Polygon' ? coords[0] : coords;
    let vertex = null;
    coords.some(coord => {
        const px = map.getPixelFromCoordinate(coord)
        const xDif = Math.abs(Math.round(pixel[0]) - Math.round(px[0]))
        const yDif = Math.abs(Math.round(pixel[1]) - Math.round(px[1]));
        if (xDif <= tolarance && yDif <= tolarance) {
            vertex = coord;
            return true;
        }
    });
    return vertex ? vertex : false;
}
