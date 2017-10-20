import React, {PropTypes, Component} from 'react'
import {GridList} from 'material-ui/GridList'
import DataPackListItem from './DataPackListItem';
import LoadButtons from './LoadButtons';
import MapPopup from './MapPopup';
import CustomScrollbar from '../CustomScrollbar';
import ol from 'openlayers';
import isEqual from 'lodash/isEqual';
import css from '../../styles/ol3map.css';
import Dot from 'material-ui/svg-icons/av/fiber-manual-record';
import SearchAOIToolbar from '../MapTools/SearchAOIToolbar.js';
import DrawAOIToolbar from '../MapTools/DrawAOIToolbar.js';
import InvalidDrawWarning from '../MapTools/InvalidDrawWarning.js';
import DropZone from '../MapTools/DropZone.js';
import {generateDrawLayer, generateDrawBoxInteraction, generateDrawFreeInteraction,
    serialize, isGeoJSONValid, createGeoJSON, createGeoJSONGeometry, zoomToExtent, clearDraw,
    MODE_DRAW_BBOX, MODE_DRAW_FREE, MODE_NORMAL, zoomToGeometry, featureToPoint,
    isViewOutsideValidExtent, goToValidExtent, unwrapCoordinates, unwrapExtent,
    isBox, isVertex, convertGeoJSONtoJSTS, jstsGeomToOlGeom } from '../../utils/mapUtils';

export const RED_STYLE = new ol.style.Style({
    stroke: new ol.style.Stroke({
        color: '#ce4427',
        width: 6,
        
    }),
    image: null,
    zIndex: Infinity,
});

export const BLUE_STYLE = new ol.style.Style({
    stroke: new ol.style.Stroke({
        color: '#4498c0',
        width: 4,
    }),
    image: null,
    zIndex: 1
});

export class MapView extends Component {
    constructor(props) {
        super(props);
        this.initMap = this.initMap.bind(this);
        this.initOverlay = this.initOverlay.bind(this);
        this.handlePopupClose = this.handlePopupClose.bind(this);
        this.addRunFeatures = this.addRunFeatures.bind(this);
        this.handleClick = this.handleClick.bind(this);
        this.handleOlPopupClose = this.handleOlPopupClose.bind(this);
        this.zoomToSelected = this.zoomToSelected.bind(this);
        this.animate = this.animate.bind(this);
        this.onMapClick = this.onMapClick.bind(this);
        this.setAllButtonsDefault = this.setAllButtonsDefault.bind(this);
        this.setButtonSelected = this.setButtonSelected.bind(this);
        this.toggleImportModal = this.toggleImportModal.bind(this);
        this.showInvalidDrawWarning = this.showInvalidDrawWarning.bind(this);
        this.handleCancel = this.handleCancel.bind(this);
        this.handleSearch = this.handleSearch.bind(this);
        this.onDrawEnd = this.onDrawEnd.bind(this);
        this.onDrawStart = this.onDrawStart.bind(this);
        this.updateMode = this.updateMode.bind(this);
        this.setMapView = this.setMapView.bind(this);
        this.defaultStyleFunction = this.defaultStyleFunction.bind(this);
        this.selectedStyleFunction = this.selectedStyleFunction.bind(this);
        this.handleUp = this.handleUp.bind(this);
        this.handleMove = this.handleMove.bind(this);
        this.handleDrag = this.handleDrag.bind(this);
        this.handleDown = this.handleDown.bind(this);
        this.bufferMapFeature = this.bufferMapFeature.bind(this);
        this.doesMapHaveDrawFeature = this.doesMapHaveDrawFeature.bind(this);
        this.state = {
            selectedFeature: null,
            groupedFeatures: [],
            showPopup: false,
            toolbarIcons: {
                box: "DEFAULT",
                free: "DEFAULT",
                mapView: "DEFAULT",
                import: "DEFAULT",
                search: "DEFAULT",
            },
            showImportModal: false,
            showInvalidDrawWarning: false,
            mode: MODE_NORMAL,
            disableMapClick: false,
        }
    }

    componentDidMount() {
        this.map = this.initMap();
        this.initOverlay();
        this.source = new ol.source.Vector({wrapX: true});
        this.layer = new ol.layer.Vector({
            source: this.source,
            style: this.defaultStyleFunction
        });
        this.drawLayer = generateDrawLayer();
        this.markerLayer = generateDrawLayer();

        this.markerLayer.setStyle(new ol.style.Style({
            image: new ol.style.Circle({
                fill: new ol.style.Fill({color: 'rgba(255,255,255,0.4)'}),
                stroke: new ol.style.Stroke({color: '#ce4427', width: 1.25}),
                radius: 5
            }),
            fill: new ol.style.Fill({color: 'rgba(255,255,255,0.4)'}),
            stroke: new ol.style.Stroke({color: '#3399CC', width: 1.25})
        }));

        this.drawBoxInteraction = generateDrawBoxInteraction(this.drawLayer);
        this.drawBoxInteraction.on('drawstart', this.onDrawStart);
        this.drawBoxInteraction.on('drawend', this.onDrawEnd);

        this.drawFreeInteraction = generateDrawFreeInteraction(this.drawLayer);
        this.drawFreeInteraction.on('drawstart', this.onDrawStart);
        this.drawFreeInteraction.on('drawend', this.onDrawEnd);

        this.pointer = new ol.interaction.Pointer({
            handleDownEvent: this.handleDown,
            handleDragEvent: this.handleDrag,
            handleMoveEvent: this.handleMove,
            handleUpEvent: this.handleUp
        });

        this.map.addInteraction(this.pointer);
        this.map.addInteraction(this.drawBoxInteraction);
        this.map.addInteraction(this.drawFreeInteraction);
        

        this.map.addLayer(this.layer);
        this.map.addLayer(this.drawLayer);
        this.map.addLayer(this.markerLayer);

        this.addRunFeatures(this.props.runs, this.source);
        this.map.getView().fit(this.source.getExtent(), this.map.getSize());
        this.clickListener = this.map.on('singleclick', this.onMapClick);
    }

    componentWillReceiveProps(nextProps) {
        // if the runs have changed, clear out old features and re-add with new features
        if (this.hasNewRuns(this.props.runs, nextProps.runs)) {
            this.source.clear();
            const added = this.addRunFeatures(nextProps.runs, this.source);
            const drawExtent = this.drawLayer.getSource().getExtent();
            const runsExtent = this.source.getExtent();
            // if any features were added to the source
            if (added) {
                // if there is a draw feature and it contains all the runs: fit to draw feature
                if (this.drawLayer.getSource().getFeatures().length && ol.extent.containsExtent(drawExtent, runsExtent)) {
                        this.map.getView().fit(drawExtent);
                }
                // if no draw feature or it does not contain all runs: just fit the runs
                else {
                    this.map.getView().fit(runsExtent);
                }
            }
            // if no features added but there is a draw feature: zoom to draw feature
            else if (this.drawLayer.getSource().getFeatures().length) {
                zoomToGeometry(this.drawLayer.getSource().getFeatures()[0].getGeometry(), this.map);
            }
        }

        if(nextProps.importGeom.processed && !this.props.importGeom.processed) {
            this.handleGeoJSONUpload(nextProps.importGeom.geom);
        }
    }

    // update map size so it doesnt look like crap after page resize
    componentDidUpdate() {
        this.map.updateSize();
    }

    hasNewRuns(prevRuns, nextRuns) {
        if (prevRuns.length != nextRuns.length) {
            return true;
        }
        else {
            for (let i=0; i < nextRuns.length; i++) {
                if (nextRuns[i].uid != prevRuns[i].uid) {
                    return true;
                }
            };
            return false
        }
    }

    // read the extents from the runs and add each feature to the source
    addRunFeatures(runs, source) {
        const reader = new ol.format.GeoJSON();
        const features = runs.map((run) => { 
            let feature = reader.readFeature(run.job.extent, {
                dataProjection: 'EPSG:4326',
                featureProjection: 'EPSG:3857'
            });
            feature.setId(run.uid);
            feature.setProperties(run);
            return feature;
        });
        if (features.length) {
            source.addFeatures(features);
            return true;
        };
        return false;
    }

    // add map with controls and basemap to the page
    initMap() {
        ol.control.ZoomExtent = zoomToExtent;
        ol.inherits(ol.control.ZoomExtent, ol.control.Control);
        return new ol.Map({
            controls: [
                new ol.control.Attribution({
                    className: ['ol-attribution', css['ol-attribution']].join(' '),
                    collapsible: false,
                    collapsed: false,
                }),
                new ol.control.Zoom({
                    className: css.olZoom
                }),
                new ol.control.ZoomExtent({
                    className: css.olZoomToExtent,
                    extent: [-14251567.50789682, -10584983.780136958, 14251787.50789682, 10584983.780136958],
                }),
                new ol.control.OverviewMap({
                    className: ['ol-overviewmap', css['ol-custom-overviewmap']].join(' '),
                    collapsible: true,
                    collapsed: window.innerWidth < 768 ? true: false,
                    collapseLabel: '\u00BB',
                    label: '\u00AB',
                }),
            ],
            interactions: ol.interaction.defaults({
                keyboard: false,
                altShiftDragRotate: false,
                pinchRotate: false
            }),
            layers: [
                new ol.layer.Tile({
                    source: new ol.source.XYZ({
                        url: this.context.config.BASEMAP_URL,
                        wrapX: true,
                        attributions: this.context.config.BASEMAP_COPYRIGHT
                    })
                }),
            ],
            target: 'map',
            view: new ol.View({
                projection: "EPSG:3857",
                center: [110, 0],
                zoom: 2,
                minZoom: 2,
                maxZoom: 22,
            })
        });
    }

    initOverlay() {
        this.container = document.getElementById('popup');
        this.content = document.getElementById('popup-content');
        this.closer = document.getElementById('popup-closer');
        this.overlay = new ol.Overlay({
            element: this.container,
            autoPan: true,
            autoPanMargin: 100,
            autoPanAnimation: {
                duration: 250
            },
            stopEvent: false
        });
        this.closer.onclick = this.handleOlPopupClose
        this.map.addOverlay(this.overlay);
    }

    handleOlPopupClose() {
        this.map.addInteraction(new ol.interaction.MouseWheelZoom());
        this.overlay.setPosition(undefined);
        this.closer.blur();
        window.setTimeout(() => {
            this.setState({disableMapClick: false});
        }, 300);
        return false;
    }

    // Called when a user clicks on a list item
    handleClick(runId) {
        if (runId) {
            const feature = this.source.getFeatureById(runId) || null;
            if (feature) {
                this.setState({showPopup: false});
                // if there is another feature already selected it needs to be deselected
                if (this.state.selectedFeature && this.state.selectedFeature != feature.getId()) {
                    const oldFeature = this.source.getFeatureById(this.state.selectedFeature);
                    this.setFeatureNotSelected(oldFeature);
                }
                // if clicked on feature is already selected it should be deselected
                if(this.state.selectedFeature && this.state.selectedFeature == feature.getId()) {
                    this.setFeatureNotSelected(feature);
                    this.setState({selectedFeature: null});
                }
                // if not already selected the feature should then be selected
                else {
                    this.setFeatureSelected(feature);
                    this.setState({selectedFeature: feature.getId(), showPopup: true});

                    // if the feature in not in current view, center the view on selected feature
                    // make sure we are comparing only unwrapped extents to avoid uneeded centering when map is wrapped
                    const mapExtent = unwrapExtent(this.map.getView().calculateExtent(), this.map.getView().getProjection());
                    const featureExtent = unwrapExtent(feature.getGeometry().getExtent(), this.map.getView().getProjection());

                    if(!ol.extent.containsExtent(mapExtent, featureExtent)) {
                        this.map.getView().setCenter(ol.extent.getCenter(featureExtent));
                    }
                    // if it is in view and not a polygon, trigger an animation
                    else {
                        if (!this.displayAsPoint(feature)) {
                            return true;
                        }
                        
                        const start = new Date().getTime();
                        const geom = feature.getGeometry();
                        if (this.listener) {
                            ol.Observable.unByKey(this.listener);
                            this.listener = null;   
                        }
                        this.listener = this.map.on('postcompose', (event) => this.animate(event, geom, start));
                        this.map.render();
                    }
                }
                return true;
            }
        }
        return false;
    }

    // animates a circle expanding out from the geometry in question
    animate(event, geom, start) {
        const vectorContext = event.vectorContext;
        const frameState = event.frameState;
        const point = new ol.geom.Point(ol.extent.getCenter(geom.getExtent()));

        const ext = geom.getExtent();
        const tl = this.map.getPixelFromCoordinate(ol.extent.getTopLeft(ext));
        const tr = this.map.getPixelFromCoordinate(ol.extent.getTopRight(ext));
        const bl = this.map.getPixelFromCoordinate(ol.extent.getBottomLeft(ext));
        const width = tr[0] - tl[0]; 
        const height = bl[1] - tl[1];
        const featureRad = Math.max(width, height);

        const elapsed = frameState.time - start;
        const elapsedRatio = elapsed / 3000;

        const radius = ol.easing.easeOut(elapsedRatio) * 25 + 5 + featureRad;
        const opacity = ol.easing.easeOut(1 - elapsedRatio);

        const style = new ol.style.Style({
            image: new ol.style.Circle({
                radius: radius,
                snapToPixel: false,
                stroke: new ol.style.Stroke({
                    color: 'rgba(255, 0, 0, ' + opacity + ')',
                    width: 0.25 + opacity
                })
            })
        });
        vectorContext.setStyle(style);
        vectorContext.drawGeometry(point);
        if(elapsed > 3000) {
            ol.Observable.unByKey(this.listener);
            return 0;
        }
        this.map.render();
    }

    onMapClick(evt) {
        if (this.state.mode != MODE_NORMAL || this.state.disableMapClick) {
            return false;
        };
        let features = [];
        this.map.forEachFeatureAtPixel(evt.pixel, (feature) => {
            if (feature.getProperties().uid) {
                features.push(feature);                
            }
        }, {hitTolerance: 3});
        if (features.length) {
            if(features.length == 1) {
                this.handleClick(features[0].getId());
            }
            else {
                this.setState({groupedFeatures: features, disableMapClick: true});
                // disable scroll zoom while popup is open
                let zoom = null;
                this.map.getInteractions().forEach((interaction) => {
                    if (interaction instanceof ol.interaction.MouseWheelZoom) {
                        zoom = interaction
                    }
                });
                if (zoom) {this.map.removeInteraction(zoom)};
                const coord = evt.coordinate;
                this.overlay.setPosition(coord);
            }
        }
        return true;
    }

    // checks the state for a selectedFeature ID and zooms to that feature
    zoomToSelected() {
        if (this.state.selectedFeature) {
            const feature = this.source.getFeatureById(this.state.selectedFeature);
            zoomToGeometry(feature.getGeometry(), this.map);
        }
    }

    // call handleClick with currently selected feature if user closes popup
    handlePopupClose() {
        this.handleClick(this.state.selectedFeature)
    }

    // helper function that changes feature style to unselected
    setFeatureNotSelected(feature) {
        feature.setStyle(this.defaultStyleFunction);
    }

    // helper function that changes feature style to selected
    setFeatureSelected(feature) {
        feature.setStyle(this.selectedStyleFunction);
    }

    displayAsPoint(feature) {
        if(!feature) {return false}
        const extent = feature.getGeometry().getExtent();
        const topLeft = this.map.getPixelFromCoordinate(ol.extent.getTopLeft(extent));
        const bottomRight = this.map.getPixelFromCoordinate(ol.extent.getBottomRight(extent));
        if(topLeft && bottomRight) {
            const height =  bottomRight[1] - topLeft[1];
            const width = bottomRight[0] - topLeft[0];
            return !((height > 10 || width > 10) && height * width >= 50);    
        }
        return true
    }

    defaultStyleFunction(feature, resolution) {
        const pointStyle = new ol.style.Style({
            geometry: featureToPoint,
            image: new ol.style.Circle({
                radius: 6,
                fill: new ol.style.Fill({
                    color: '#4598bf',
                }),
                stroke: new ol.style.Stroke({
                    color: '#fff',
                    width: 2
                }),
            }),
            zIndex: 1
        });
        if(this.displayAsPoint(feature)) {
            return pointStyle;
        }
        return BLUE_STYLE;
    }

    selectedStyleFunction(feature, resolution) {
        const pointStyle = new ol.style.Style({
            geometry: featureToPoint,
            image: new ol.style.Circle({
                radius: 6,
                fill: new ol.style.Fill({
                    color: '#ce4427'
                }),
                stroke: new ol.style.Stroke({
                    color: '#fff',
                    width: 2
                }),
            }),
            zIndex: Infinity
        });
        if(this.displayAsPoint(feature)) {
            return pointStyle;
        }
        return RED_STYLE;
    }

    handleSearch(result) {
        clearDraw(this.drawLayer);
        this.showInvalidDrawWarning(false);
        const feature = (new ol.format.GeoJSON()).readFeature(result);
        feature.getGeometry().transform('EPSG:4326', 'EPSG:3857');
        this.drawLayer.getSource().addFeature(feature);
        const geojson_geometry = createGeoJSONGeometry(feature.getGeometry());
        this.props.onMapFilter(geojson_geometry);
        if (this.source.getFeatures().length == 0) {
            zoomToGeometry(feature.getGeometry(), this.map);
        }
        return true;
    }

    handleCancel() {
        this.showInvalidDrawWarning(false);
        if(this.state.mode != MODE_NORMAL) {
            this.updateMode(MODE_NORMAL);
        }
        clearDraw(this.drawLayer);
        // remove filter
        this.props.onMapFilter(null);
    }

    setButtonSelected(iconName) {
        const icons = {...this.state.toolbarIcons};
        Object.keys(icons).forEach((key) => {
            if (key == iconName) {
                icons[key] = 'SELECTED';
            }
            else {
                icons[key] = 'INACTIVE';
            }
        });
        this.setState({toolbarIcons: icons});
    }

    setAllButtonsDefault() {
        const icons = {...this.state.toolbarIcons};
        Object.keys(icons).forEach((key) => {
            icons[key] = 'DEFAULT';
        });
        this.setState({toolbarIcons: icons});
    }

    toggleImportModal(show) {
        if (show != undefined) {
            this.setState({showImportModal: show});
        }
        else {
            this.setState({showImportModal: !this.state.showImportModal});
        }
    }

    showInvalidDrawWarning(show) {
        if (show != undefined) {
            this.setState({showInvalidDrawWarning: show});
        }
        else {
            this.setState({showInvalidDrawWarning: !this.state.showInvalidDrawWarning});
        }
    }

    onDrawStart() {
        clearDraw(this.drawLayer);
        this.setState({disableMapClick: true});
    }

    onDrawEnd(event) {
        // get the drawn geometry
        const geom = event.feature.getGeometry();
        const coords = geom.getCoordinates();
        const unwrappedCoords = unwrapCoordinates(coords, this.map.getView().getProjection());
        geom.setCoordinates(unwrappedCoords);
        const geojson = createGeoJSON(geom);
        const bbox = geojson.features[0].bbox;
        //make sure the user didnt create a polygon with no area
        if(bbox[0] != bbox[2] && bbox[1] != bbox[3]) {
            if (this.state.mode == MODE_DRAW_FREE) {
                const feature = new ol.Feature({geometry: geom});
                this.drawLayer.getSource().addFeature(feature);
                if(isGeoJSONValid(geojson)) {
                    const geojson_geometry = createGeoJSONGeometry(geom);
                    this.props.onMapFilter(geojson_geometry);
                }
                else {
                    this.showInvalidDrawWarning(true);
                }
            }
            else if (this.state.mode = MODE_DRAW_BBOX) {
                const geojson_geometry = createGeoJSONGeometry(geom);
                this.props.onMapFilter(geojson_geometry);
            }
            this.updateMode(MODE_NORMAL);
            window.setTimeout(() => {
                this.setState({disableMapClick: false});
            }, 300);
        }
    }

    setMapView() {
        clearDraw(this.drawLayer);
        const extent = this.map.getView().calculateExtent(this.map.getSize());
        const geom = new ol.geom.Polygon.fromExtent(extent);
        const coords = geom.getCoordinates();
        const unwrappedCoords = unwrapCoordinates(coords, this.map.getView().getProjection());
        geom.setCoordinates(unwrappedCoords);
        const feature = new ol.Feature({
            geometry: geom
        });
        this.drawLayer.getSource().addFeature(feature);
        const geojson_geometry = createGeoJSONGeometry(geom);
        this.props.onMapFilter(geojson_geometry);
    }

    updateMode(mode, callback) {
        // make sure interactions are deactivated
        this.drawBoxInteraction.setActive(false);
        this.drawFreeInteraction.setActive(false);
        if (isViewOutsideValidExtent(this.map.getView())) {
            // Even though we can 'wrap' the draw layer and 'unwrap' the draw coordinates
            // when needed, the draw interaction breaks if you wrap too many time, so to 
            // avoid that issue we go back to the valid extent but maintain the same view
            goToValidExtent(this.map.getView());
        };
        // if box or draw activate the respective interaction
        if(mode == MODE_DRAW_BBOX) {
            this.drawBoxInteraction.setActive(true);
        }
        else if(mode == MODE_DRAW_FREE) {
            this.drawFreeInteraction.setActive(true);
        }
        // update the state
        this.setState({mode: mode}, callback);
    }

    handleGeoJSONUpload(geom) {
        clearDraw(this.drawLayer);
        this.drawLayer.getSource().addFeature(
            new ol.Feature({
                geometry: geom
            })
        );
        zoomToGeometry(geom, this.map);
        const geojson_geometry = createGeoJSONGeometry(geom);
        this.props.onMapFilter(geojson_geometry);
    }

    bufferMapFeature(size) {
        const feature = this.drawLayer.getSource().getFeatures()[0];
        const geom = feature.getGeometry();

        const geojson = createGeoJSON(geom);
        const bufferedFeature = convertGeoJSONtoJSTS(geojson, size, true);
        if (bufferedFeature.getArea() === 0) {
            return false;
        }

        const newGeom = jstsGeomToOlGeom(bufferedFeature);
        const newFeature = feature.clone();
        const newGeojsonGeom = createGeoJSONGeometry(newGeom);
        newFeature.setGeometry(newGeom);
        clearDraw(this.drawLayer);
        this.drawLayer.getSource().addFeature(newFeature);
        this.props.onMapFilter(newGeojsonGeom);

        return true;
    }

    doesMapHaveDrawFeature() {
        if (!this.drawLayer) {
            return false;
        }
        return this.drawLayer.getSource().getFeatures().length > 0;
    }

    handleUp(evt) {
        const feature = this.feature;
        if (feature) {
            const geom = feature.getGeometry();
            const coords = geom.getCoordinates();
            const unwrappedCoords = unwrapCoordinates(coords, this.map.getView().getProjection());
            geom.setCoordinates(unwrappedCoords);
            const geojson = createGeoJSON(geom);
            if (isGeoJSONValid(geojson)) {
                const geojson_geometry = createGeoJSONGeometry(geom);
                this.props.onMapFilter(geojson_geometry);
                this.showInvalidDrawWarning(false);
            }
            else {
                this.showInvalidDrawWarning(true);
            }
        }
        this.coordinate = null;
        this.feature = null;
        return false;
    }

    handleDrag(evt) {
        const deltaX = evt.coordinate[0] - this.coordinate[0];
        const deltaY = evt.coordinate[1] - this.coordinate[1];
        const feature = this.feature;
        let coords = feature.getGeometry().getCoordinates()[0];
         // create new coordinates for the feature based on new drag coordinate
        if (isBox(feature)) {
            coords = coords.map(coord => {
                let newCoord = [...coord]
                if (coord[0] == this.coordinate[0]) {
                    newCoord[0] = evt.coordinate[0];
                }
                if (coord[1] == this.coordinate[1]) {
                    newCoord[1] = evt.coordinate[1];
                }
                return newCoord;
            });
        }
        else {
            coords = coords.map(coord => {
                let newCoord = [...coord];
                if (coord[0] == this.coordinate[0] && coord[1] === this.coordinate[1]) {
                    newCoord = [...evt.coordinate];
                }
                return newCoord;
            });
        }
        const bounds = ol.extent.boundingExtent(coords);
        // do not update the feature if it would have no area
        if(bounds[0] == bounds[2] || bounds[1] == bounds[3]) {
            return false;
        }
        feature.getGeometry().setCoordinates([coords]);
        clearDraw(this.markerLayer);
        this.markerLayer.getSource().addFeature(new ol.Feature({geometry: new ol.geom.Point(evt.coordinate)}));
        this.coordinate = [...evt.coordinate];
        return true;
    }

    handleMove(evt) {
        const map = evt.map;
        const pixel = evt.pixel;
        if(this.markerLayer.getSource().getFeatures().length > 0) {
            clearDraw(this.markerLayer);
        }
        const opts = {layerFilter: (layer) => {return layer == this.drawLayer}};
        if (map.hasFeatureAtPixel(pixel, opts)) {
            const feature = map.getFeaturesAtPixel(pixel, opts)[0];
            if (feature.getGeometry().getType() == 'Polygon') {
                if (isViewOutsideValidExtent(this.map.getView())) {
                    goToValidExtent(this.map.getView());
                };
                const coords = isVertex(pixel, feature, 10, map);
                if(coords) {
                    this.markerLayer.getSource().addFeature(new ol.Feature({geometry: new ol.geom.Point(coords)}));
                }
            }
        }
    }

    handleDown(evt) {
        const map = evt.map;
        const pixel = evt.pixel;
        const opts = {layerFilter: (layer) => {return layer == this.drawLayer}};
        if (map.hasFeatureAtPixel(pixel, opts)) {
            const feature = map.getFeaturesAtPixel(pixel, opts)[0]; 
            if (feature.getGeometry().getType() == 'Polygon') {
                const vertex = isVertex(pixel, feature, 10, map);
                if(vertex) {
                    this.feature = feature;
                    this.coordinate = vertex;
                    return true;
                }
            }           
        }
        return false;
    }

    render() {
        const spacing = window.innerWidth > 575 ? '10px' : '2px';
        const styles = {
            root: {
                display: 'flex',
                flexWrap: 'wrap',
                justifyContent: 'space-around',
                marginLeft: '10px',
                marginRight: '10px',
                paddingBottom: '10px'
            },
            map: window.innerWidth < 768 ? 
                {
                    width: '100%', 
                    height: '100%', 
                    display: 'block', 
                    overflow: 'hidden', 
                    padding: `0px ${spacing} ${spacing}`, 
                    position: 'relative'
                } 
            :
                {
                    width: '70%', 
                    height: window.innerHeight - 241,
                    display: 'inline-block', 
                    overflow: 'hidden', 
                    padding: '0px 10px 0px 3px',
                    position: 'relative'
                },
            list: window.innerWidth < 768 ?
                {
                    display: 'none'
                }
            :
                {
                    height: window.innerHeight - 241,
                    width: '30%', 
                    display: 'inline-block'
                },
            popupContainer: {
                position: 'absolute',
                width: `calc(100% - ${window.innerWidth < 768 ? 20 : 13}px)`, 
                bottom: '50px', 
                textAlign: 'center', 
                display: 'relative', 
                zIndex: 1
            }
        };

        const load = <LoadButtons
                range={this.props.range}
                handleLoadLess={this.props.handleLoadLess}
                handleLoadMore={this.props.handleLoadMore}
                loadLessDisabled={this.props.loadLessDisabled}
                loadMoreDisabled={this.props.loadMoreDisabled}
            />
        
        const feature = this.state.selectedFeature ? this.source.getFeatureById(this.state.selectedFeature): null;
        const showBuffer = this.doesMapHaveDrawFeature();
        return (
            <div style={{height: window.innderWidth > 525 ? window.innerHeight - 236 : window.innerHeight - 223}}>
                <CustomScrollbar style={styles.list}>
                    <div style={styles.root}>
                        <GridList
                            className={'qa-MapView-GridList'}
                            cellHeight={'auto'}
                            cols={1}
                            padding={0}
                            style={{width: '100%'}}
                        >   
                        {this.props.runs.map((run) => (
                            <DataPackListItem 
                                run={run} 
                                user={this.props.user} 
                                key={run.uid}
                                onRunDelete={this.props.onRunDelete}
                                onClick={this.handleClick}
                                backgroundColor={this.state.selectedFeature == run.uid ? '#dedfdf': null}
                                providers={this.props.providers}
                            />
                        ))}
                        </GridList>
                    </div>
                    {load}
                </CustomScrollbar>
                <div  style={styles.map}>
                    <div className={'qa-MapView-div-map'} style={{width: '100%', height: '100%', position: 'relative'}} id='map'>
                    <SearchAOIToolbar
                        handleSearch={this.handleSearch}
                        handleCancel={this.handleCancel}
                        geocode={this.props.geocode}
                        toolbarIcons={this.state.toolbarIcons}
                        getGeocode={this.props.getGeocode}
                        setAllButtonsDefault={this.setAllButtonsDefault}
                        setSearchAOIButtonSelected={() => {this.setButtonSelected('search')}}
                    />
                    <DrawAOIToolbar
                        toolbarIcons={this.state.toolbarIcons}
                        updateMode={this.updateMode}
                        handleCancel={this.handleCancel}
                        setMapView={this.setMapView}
                        setAllButtonsDefault={this.setAllButtonsDefault}
                        setBoxButtonSelected={() => {this.setButtonSelected('box')}}
                        setFreeButtonSelected={() => {this.setButtonSelected('free')}}
                        setMapViewButtonSelected={() => {this.setButtonSelected('mapView')}}
                        setImportButtonSelected={() => {this.setButtonSelected('import')}}
                        setImportModalState={this.toggleImportModal}
                        showBufferButton={showBuffer}
                        onBufferClick={this.bufferMapFeature}
                    />
                    <InvalidDrawWarning
                        show={this.state.showInvalidDrawWarning}
                    />
                    <DropZone
                        importGeom={this.props.importGeom}
                        showImportModal={this.state.showImportModal}
                        setAllButtonsDefault={this.setAllButtonsDefault}
                        setImportModalState={this.toggleImportModal}
                        processGeoJSONFile={this.props.processGeoJSONFile}
                        resetGeoJSONFile={this.props.resetGeoJSONFile}
                    />
                    </div>
                    <div id="popup" className={css.olPopup}>
                        <a href="#" id="popup-closer" className={css.olPopupCloser}/>
                        <div className={'qa-MapView-div-popupContent'} id="popup-content">
                            <p style={{color: 'grey'}}>Select One:</p>
                            <CustomScrollbar autoHeight autoHeightMin={20} autoHeightMax={200}>
                            {this.state.groupedFeatures.map((feature, ix) => {
                                return <a 
                                    key={ix}
                                    onClick={() => {
                                        this.handleClick(feature.getId()); 
                                        this.closer.onclick();
                                    }}
                                    style={{display: 'block', cursor: 'pointer'}}
                                >
                                    <Dot style={{opacity: '0.5', color: '#ce4427', backgroundColor: 'white', border: '1px solid #4598bf', borderRadius: '100%', height: '14px', width: '14px', verticalAlign: 'middle', marginRight: '5px'}}/> {feature.getProperties().name}
                                </a>
                            })}
                            </CustomScrollbar>
                        </div>
                    </div>
                    {this.state.showPopup && feature ?
                        <div style={styles.popupContainer}>
                            <div style={{margin: '0px auto', width: '70%', maxWidth: window.innerWidth < 768 ? '90%' : '455px', minWidth: '250px', display: 'inline-block', textAlign: 'left'}}>
                                <MapPopup
                                    featureInfo={feature.getProperties()} 
                                    detailUrl={`/status/${feature.getProperties().job.uid}`}
                                    handleZoom={this.zoomToSelected}
                                    handlePopupClose={this.handlePopupClose}
                                />
                            </div>
                        </div>
                    :
                        null
                    }
                </div>
            </div>
        )      
    }
}

MapView.contextTypes = {
    config: React.PropTypes.object
}

MapView.propTypes = {
    runs: PropTypes.array.isRequired,
    user: PropTypes.object.isRequired,
    onRunDelete: PropTypes.func.isRequired,
    range: PropTypes.string.isRequired,
    handleLoadLess: PropTypes.func.isRequired,
    handleLoadMore: PropTypes.func.isRequired,
    loadLessDisabled: PropTypes.bool.isRequired,
    loadMoreDisabled: PropTypes.bool.isRequired,
    providers: PropTypes.array.isRequired,
    geocode: PropTypes.object.isRequired,
    getGeocode: PropTypes.func.isRequired,
    importGeom: PropTypes.object.isRequired,
    processGeoJSONFile: PropTypes.func.isRequired,
    resetGeoJSONFile: PropTypes.func.isRequired,
    onMapFilter: PropTypes.func.isRequired,
};

export default MapView;
