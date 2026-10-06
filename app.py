import json 
import requests 
import pandas as pd 
import geopandas as gpd 
import pydeck as pdk 
import folium
import streamlit as st 
from folium.plugins import Draw 
from shapely.geometry import box 
from streamlit_folium import st_folium 

st.set_page_config(page_title="EV Charging Coverage Map", layout="wide")
st.title("EV Charging Station Coverage Map")

OVERPASS_URLS = [
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
    "https://overpass.private.coffee/api/interpreter",
]
NOMINATIM_URL = "https://nominatim.openstreetmap.org/search"

@st.cache_data(ttl=86400, show_spinner="looking for the city")
def geocode(city: str):
    try:
        r = requests.get(
            NOMINATIM_URL,
            params={"q": city, "format": "json", "limit": 1},
            headers={"User-Agent": "ev-coverage-learning-app"}, timeout=30,
        )
        r.raise_for_status()
    except requests.exceptions.RequestException as e:
        return {"error": f"{type(e).__name__}: {e}"}
    hits = r.json()
    if not hits:
        return None 
    s, n, w, e = map(float, hits[0]["boundingbox"])
    return s, w, n, e


@st.cache_data(ttl=3600, show_spinner="Querying OpenStreetMap...")
def fetch_stations(south, west, north, east) -> pd.DataFrame:
    query = (
        f'[out:json][timeout:60];'
        f'nwr["amenity"="charging_station"]({south},{west},{north},{east});'
        f'out center tags;'
    )
    headers = {"User-Agent": "caleb-ev-coverage-dashboard/1.0 (student portfolio project)"}

    errors = []
    data = None
    for url in OVERPASS_URLS:
        try:
            r = requests.post(url, data={"data": query}, headers=headers, timeout=90)
        except requests.exceptions.RequestException as e:
            errors.append(f"{url}: {type(e).__name__}")
            continue
        if r.ok:
            data = r.json()
            break
        errors.append(f"{url}: {r.status_code} {r.text[:200]!r}")

    if data is None:
        raise RuntimeError("All Overpass servers failed:\n" + "\n".join(errors))

    rows = []
    for el in data["elements"]:
        lat = el.get("lat") or el.get("center", {}).get("lat")
        lon = el.get("lon") or el.get("center", {}).get("lon")
        if lat is None or lon is None:
            continue
        tags = el.get("tags", {})
        rows.append({
            "lat": lat,
            "lon": lon,
            "operator": tags.get("operator", "Unknown"),
            "capacity": tags.get("capacity", "?"),
        })
    return pd.DataFrame(rows, columns=["lat", "lon", "operator", "capacity"])


@st.cache_data(show_spinner="Computing coverage...")
def compute_coverage(df: pd.DataFrame, bbox: tuple, radius_m: int):
    south, west, north, east = bbox
    gdf = gpd.GeoDataFrame(
        df, geometry=gpd.points_from_xy(df.lon, df.lat), crs="EPSG:4326"
    )

    metric_crs = gdf.estimate_utm_crs()        # picks the right UTM zone automatically
    stations_m = gdf.to_crs(metric_crs)
    area_m = gpd.GeoSeries([box(west, south, east, north)], crs="EPSG:4326").to_crs(metric_crs)

    covered = stations_m.buffer(radius_m).union_all()   # merge overlapping circles
    covered = covered.intersection(area_m.iloc[0])      # clip to the search box

    pct = covered.area / area_m.iloc[0].area * 100
    covered_geojson = gpd.GeoSeries([covered], crs=metric_crs).to_crs("EPSG:4326").to_json()
    return pct, covered_geojson
                                                                 
with st.sidebar: 
    st.header("controls")
    city = st.text_input("City", "Rowland Heights, California")      
    radius = st.slider("coverage radius (meters)", 250, 3000, 1000, step=250)
    show_heat = st.checkbox("heatmap", True)
    show_points = st.checkbox("stations", True)
    show_cov = st.checkbox("coverage area", True)
    use_drawn = st.checkbox("use box I drew on the Follium tab", False)

if "drawn_bbox" not in st.session_state:
    st.session_state.drawn_bbox = None 
bbox = geocode(city)
if isinstance(bbox, dict):
    st.error(f"Could not reach Nominatim. {bbox['error']}")
    st.stop()
if use_drawn and st.session_state.drawn_bbox:
    bbox = st.session_state.drawn_bbox
if bbox is None:
    st.error("City not found.")
    st.stop()

south, west, north, east = bbox
df = fetch_stations(south, west, north, east)

if df.empty:
    st.warning("No charging stations found in this area.")
    st.stop()

pct, covered_geojson = compute_coverage(df, bbox, radius)

c1, c2, c3 = st.columns(3)
c1.metric("Stations", len(df))
c2.metric(f"Area within {radius} m", f"{pct:.1f}%")
c3.metric("Operators", df["operator"].nunique())

tab_deck, tab_folium = st.tabs(["PyDeck map", "Folium: draw a search box"])

with tab_deck:
    layers = []
    if show_cov:
        layers.append(pdk.Layer(
            "GeoJsonLayer", data=json.loads(covered_geojson),
            get_fill_color=[0, 200, 120, 50], stroked=False,
        ))
    if show_heat:
        layers.append(pdk.Layer(
            "HeatmapLayer", data=df, get_position="[lon, lat]",
            radius_pixels=60, opacity=0.7,
        ))
    if show_points:
        layers.append(pdk.Layer(
            "ScatterplotLayer", data=df, get_position="[lon, lat]",
            get_fill_color=[255, 80, 40, 200],
            get_radius=40, radius_min_pixels=4, pickable=True,
        ))

    view = pdk.ViewState(
        latitude=(south + north) / 2, longitude=(west + east) / 2, zoom=11, pitch=0
    )
    st.pydeck_chart(pdk.Deck(
        layers=layers, initial_view_state=view,
        tooltip={"text": "{operator}\nPorts: {capacity}"},
    ))


with tab_folium:
    st.caption("Draw a rectangle, then tick 'Use box I drew' in the sidebar.")
    m = folium.Map(location=[(south + north) / 2, (west + east) / 2], zoom_start=12)
    Draw(
        export=False,
        draw_options={"rectangle": True, "polyline": False, "polygon": False,
                      "circle": False, "marker": False, "circlemarker": False},
    ).add_to(m)

    out = st_folium(m, height=500, width=None, returned_objects=["last_active_drawing"])

    drawing = out.get("last_active_drawing") if out else None
    if drawing:
        ring = drawing["geometry"]["coordinates"][0]
        lons, lats = [p[0] for p in ring], [p[1] for p in ring]
        new_bbox = (min(lats), min(lons), max(lats), max(lons))
        if new_bbox != st.session_state.drawn_bbox:
            st.session_state.drawn_bbox = new_bbox
            st.rerun()