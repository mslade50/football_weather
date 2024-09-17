import streamlit as st
import pandas as pd
import plotly.express as px

# Load your Excel file from your GitHub repository
url = "https://github.com/mslade50/football_weather/blob/main/cfb_weather.xlsx"  # Modify with your repo details
df = pd.read_excel(url)

# Split game_loc into lat and lon
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

# Create dot size based on 'gs_fg'
df['dot_size'] = df['gs_fg'].abs()

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
        return 'green'

df['dot_color'] = df.apply(assign_dot_color, axis=1)

# Create the map using Plotly
fig = px.scatter_mapbox(
    df,
    lat="lat",
    lon="lon",
    hover_name="Game",
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
    size="dot_size",
    color="dot_color",
    color_discrete_map={
        'red': 'red',
        'lightblue': 'lightblue',
        'purple': 'purple',
        'yellow': 'yellow',
        'green': 'green'
    },
    zoom=3,
    height=700,
)

fig.update_layout(mapbox_style="open-street-map")

# Display in Streamlit
st.title("College Football Weather Map")
st.plotly_chart(fig)

# Optional: Display game details in sidebar
if st.sidebar.checkbox("Show game details", False):
    game = st.sidebar.selectbox("Select a game", df['Game'].unique())
    selected_game = df[df['Game'] == game]
    
    if not selected_game.empty:
        st.write(f"Details for {game}")
        st.table(selected_game[['wind_fg', 'temp_fg', 'rain_fg', 'Fd_open', 'FD_now', 'game_loc', 'wind_diff', 'wind_vol']])
