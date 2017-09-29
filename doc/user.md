##DataPack Library

All DataPacks that are visible to the user are displayed here, in a list or tiled view. Users may filter the displayed DataPacks using the controls on the right side, specifying them by permissions, export status, date, and sources they include.

##Creating a DataPack

To create a DataPack, select "Create DataPack" in the left sidebar, or to the top right of the DataPack Library view.

**Step 1 of 3: Define Area of Interest**

The first screen allows a user to select an area of interest for the Datapack, to serve as a bounding box and cutline for the data sources. Raster and vector data will be contained completely within this area, and vector features that partially intersect the area will be cut appropriately.

**Step 2 of 3: Select Data & Formats**

After the area of interest is selected, enter a name for the DataPack, a description, and a project name with which to associate it.

There is an option to make the DataPack publicly available to other users; this may be changed after the export.

All of the available export providers are listed, and at least one must be selected. Each selected source will contribute to an individual GeoPackage or GeoTIFF, all of which will be packaged together in a zip file for convenience.

Currently, the only projection available is EPSG:4326, or the WGS84 equirectangular spatial referencing system. Also, each provider will only export in the GeoPackage format, or GeoTIFF for WCS providers.

**Step 3 of 3: Review & Submit**

The export's name, description, project, visibility, formats, selected layers, and AOI are shown on this last page, allowing a user to confirm each of them is correct before submitting the export job.

##Status & Download

After the export job is submitted, EventKit will display the Status & Download page, which contains general information about the export and its current status, access controls, clone/delete options, and download options once the job has completed.

###Status

**Export** - May be one of the following:

* COMPLETED: All tasks completed successfully.
* INCOMPLETE: One or more tasks were unsuccessful.
* PENDING: None of the tasks have been started yet.
* RUNNING: One or more tasks have been started.
* CANCELED: Tasks have been canceled by the user.
* FAILED: All format tasks were unsuccessful.

**Expiration** - The date on which this DataPack will expire. This defaults to ten days after creation, but can be changed by a user at any time. The submitter will receive an email warning one week before expiration, or two days before expiration if the datapack's lifespan is less than a week.

**Permission** - Can be set to Public or Private, and may be changed at any time by a user. If Public, this DataPack will be displayed in the Library for all users; if Private, only the submitting user and administrators will have access.

###Download Options

Once the download is complete, each of the layers included in the DataPack will be displayed here. Users can download them individually as GeoPackages or GeoTIFF files by clicking the dropdown icon on the right of each layer. All the layers may also be downloaded together as a zipfile, with the DOWNLOAD DATAPACK (.ZIP) button above the layers.

###Other Options

* Run Export Again: Stop tasks related to this export, remove output files, and begin the export process again.
* Clone: Create a new DataPack with the same AOI, name, and other parameters as the current one. These may be edited before submitting the export.
* Delete: Remove the DataPack and all associated files.

###General Information

This area displays general information about the DataPack, including its description, project, selected data sources, file format, and projection, each with more detailed information available as a hover icon. The file format is determined by the export provider type, and will be GeoPackage for all of them except WCS, which exports in the GeoTIFF format.

###Selected Area of Interest

The Area of Interest (AOI) that the user specified for the export is displayed on an interactive map here.

###Export Information

Information related to the export (not to the resulting DataPack) is displayed here, including the user that submitted the run, the ID, and its start/finish dates.

