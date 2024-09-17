import streamlit as st
import pandas as pd
import plotly.express as px

# Enable wide mode for the page
st.set_page_config(layout="wide")

# Load your Excel file
df = pd.read_excel('cfb_weather.xlsx', engine='openpyxl')
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

# Process data for map
df['dot_size'] = df['gs_fg'].abs()  # Create dot size based on 'gs_fg'

# Assign dot color based on conditions
def assign_dot_color(row):
    if row['temp_fg'] > 80 and row['wind_fg'] < 12:
        return 'red'
    elif row['temp_fg'] < 30 and row['wind_fg'] < 12:
        return 'lightblue'
    elif row['wind_fg'] > 12:
        return 'purple'
    elif row['rain_fg'] > 0 and row['wind_fg'] < 12:
        return 'yellow'
    else:
        return 'green'  # Default color

df['dot_color'] = df.apply(assign_dot_color, axis=1)

# Create the map using Plotly
fig = px.scatter_mapbox(
    df,
    lat="lat",  # Use the newly created 'lat' column
    lon="lon",  # Use the newly created 'lon' column
    hover_name="Game",  # Column to show on hover
    hover_data={
        "wind_fg": True,
        "temp_fg": True,
        "rain_fg": True,
        "Fd_open": True,
        "FD_now": True,
        "game_loc": True,
        "wind_diff": True,
        "wind_vol": True,
    },
    size="dot_size",  # Use the 'gs_fg' field for dot size
    color="dot_color",  # Color based on conditions
    color_discrete_map={
        'red': 'Heat',
        'lightblue': 'Cold',
        'purple': 'Wind',
        'yellow': 'Rain',
        'green': 'N/A'
    },
    zoom=4,  # Adjusted for better zoom in the US
    height=800,  # Make the map occupy a larger portion of the page
)

# Update the layout to focus on the US and adjust map display
fig.update_layout(
    mapbox_style="open-street-map",
    mapbox_center={"lat": 37.0902, "lon": -95.7129},  # Center the map in the U.S.
    mapbox_zoom=3.5,  # Zoom to focus on U.S. only
)

# Display in Streamlit with wide layout
st.title("College Football Weather Map")
st.plotly_chart(fig, use_container_width=True)

# When a dot is clicked, show additional details
if st.sidebar.checkbox("Show game details", False):
    game = st.sidebar.selectbox("Select a game", df['Game'].unique())
    selected_game = df[df['Game'] == game]
    
    if not selected_game.empty:
        st.write(f"Details for {game}")
        st.table(selected_game[['wind_fg', 'temp_fg', 'rain_fg', 'Fd_open', 'FD_now', 'game_loc', 'wind_diff', 'wind_vol']])
