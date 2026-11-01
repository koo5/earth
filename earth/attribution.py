"""What the data served is, whose it is, and what its licence asks of us: as DATA per response,
not as UI copy.

Serving tiles is distributing the data itself, not only renders. Each tile says which sources its
samples came from (X-Tile-Sources, and the source byte per sample); /attribution says what each
of those requires shown.
"""
from __future__ import annotations

# source name (tiles.SOURCES) -> the attribution it requires
SOURCES: dict[str, dict] = {
    "cuzk": {
        "name": "ČÚZK DMR 5G / DMP 1G",
        "text": "© ČÚZK (DMR 5G / DMP 1G, CC BY 4.0)",
        "licence": "CC BY 4.0",
        "url": "https://geoportal.cuzk.cz",
    },
    "cuzk-ortho": {
        "name": "ČÚZK Ortofoto ČR",
        # the terms' own format for a viewing service: "ČÚZK, <year>", the year of the imagery,
        # which is the release's (GET /releases/{id}: sources.year)
        "text": "ČÚZK, {year}",
        "licence": "CC BY 4.0",
        "url": "https://geoportal.cuzk.gov.cz",
        "terms_url": "https://cuzk.gov.cz/Predpisy/Podminky-poskytovani-prostor-dat-a-sitovych-sluzeb/Podminky-poskytovani-prostorovych-dat-CUZK.aspx",
        "metadata_url": "https://geoportal.cuzk.gov.cz/SDIProCSW/service.svc/get?REQUEST=GetRecordById&SERVICE=CSW&VERSION=2.0.2&OUTPUTSCHEMA=http://www.isotc211.org/2005/gmd&ELEMENTSETNAME=full&Id=CZ-CUZK-ORTOFOTO-R",
    },
    "ipr": {
        "name": "IPR Praha, Budovy 3D (2020)",
        "text": "datový podklad © IPR Praha",
        "licence": "CC BY 4.0",
        "url": "https://geoportalpraha.cz/data-a-sluzby/afaedcf7e8b94ed5b1eb4362263bec98",
        "terms_url": "https://geoportalpraha.cz/data-a-sluzby/otevrena-data",
        # CC BY 4.0 asks the changes to be said: every manifest's credit.changes says them too
        "changes": "I3S to GLB; EGM96 heights converted to WGS84 ellipsoidal heights",
    },
    "osm": {
        "name": "OpenStreetMap",
        "text": "© OpenStreetMap contributors",
        "licence": "ODbL 1.0",
        "url": "https://www.openstreetmap.org/copyright",
    },
    "glo30": {
        "name": "Copernicus WorldDEM-30 (GLO-30)",
        # licence 6(b), "produced using": earth serves GLO-30 resampled onto its tile grid, merged with ČÚZK's
        # heights and quantised, not the data as delivered; and the one line then covers what is built on these
        # tiles too (6(a) is the notice without the prefix)
        "text": "produced using Copernicus WorldDEM-30 © DLR e.V. 2010-2014 and © Airbus Defence and Space GmbH "
                "2014-2018 provided under COPERNICUS by the European Union and ESA; all rights reserved",
        "licence": "Copernicus DEM licence (GLO-30 public)",
        "url": "https://spacedata.copernicus.eu/collections/copernicus-digital-elevation-model",
        # 6(c): the liability sentence, which must also stand in the public terms of the service
        "terms": "The organisations in charge of the Copernicus programme by law or by delegation do "
                 "not incur any liability for any use of the Copernicus WorldDEM-30",
    },
    "etopo": {
        "name": "ETOPO 2022 Global Relief Model (60 arc-second)",
        "text": "NOAA National Centers for Environmental Information. 2022: ETOPO 2022 Global Relief "
                "Model. doi:10.25921/fd45-gt74",
        "licence": "US government work, public domain",
        "url": "https://www.ncei.noaa.gov/products/etopo-global-relief-model",
    },
    "egm2008": {
        "name": "EGM2008 geoid",
        "text": "EGM2008 geoid, US National Geospatial-Intelligence Agency",
        "licence": "public domain",
        "url": "https://earth-info.nga.mil",
    },
}

# tiles.SOURCES name -> attribution key
OF_TILE_SOURCE = {"dtm10.vrt": "cuzk", "dsm10.vrt": "cuzk", "dtm2.vrt": "cuzk", "dsm2.vrt": "cuzk",
                  "dtm10.fill": "cuzk", "dtm2.fill": "cuzk",
                  "glo30.vrt": "glo30", "etopo60": "etopo", "sea": "etopo"}


def for_tile(used: dict[str, int]) -> list[str]:
    """The attribution keys a tile with these sources needs; every tile's heights carry EGM2008."""
    keys = sorted({OF_TILE_SOURCE[s] for s in used if s in OF_TILE_SOURCE})
    return keys + ["egm2008"]
