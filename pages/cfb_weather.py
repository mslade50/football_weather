import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

st.set_page_config(layout="wide")

# Load your Excel file
df = pd.read_excel('cfb_weather.xlsx', engine='openpyxl')
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')

# Define the signals for dot size and color
def assign_signal(row):
    if (row['wind_fg'] > 8 and row['temp_fg'] < 75) or (row['rain_fg'] > 2):
        return 'Low Impact'
    elif (row['wind_fg'] > 15 and row['temp_fg'] < 75) or (row['travel_alt'] > 900 and row['temp_fg'] > 75):
        return 'Mid Impact'
    elif row['wind_fg'] > 15 and row['temp_fg'] < 50:
        return 'High Impact'
    else:
        return 'No Impact'

# Assign signal and color
df['signal'] = df.apply(assign_signal, axis=1)
df['dot_color'] = df['signal'].map({
    'Low Impact': 'blue',
    'Mid Impact': 'orange',
    'High Impact': 'purple',
    'No Impact': 'green'
})

# Assign dot sizes based on the signal
df['dot_size'] = df['signal'].map({
    'Low Impact': 15,
    'Mid Impact': 25,
    'High Impact': 40,
    'No Impact': 7
})

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
        "gs_fg": True,
        "Fd_open": True,
        "FD_now": True,
        "game_loc": True,
        "wind_diff": True,
        "wind_vol": True,
        "My_total": True,
        "Edge": True,
        "Open": True,
        "Current": True,
        "away_fg": True  # Added away_fg to hover data
    },
    size="dot_size",
    color="dot_color",
    color_discrete_map={
        'blue': 'blue',
        'orange': 'orange',
        'purple': 'purple',
        'green': 'green',
    },
    zoom=6,
    height=1000,
)
fig.update_layout(
    mapbox_style="open-street-map",
    mapbox_center={"lat": 37.0902, "lon": -95.7129},  # Center the map in the U.S.
    mapbox_zoom=3.5,  # Zoom to focus on U.S. only
    legend_title_text='Weather Conditions',  # Set custom legend title
)
# Update the legend labels for the colors
fig.for_each_trace(
    lambda t: t.update(
        name=t.name.replace('blue', 'Low Impact')
                   .replace('orange', 'Mid Impact')
                   .replace('purple', 'High Impact')
                   .replace('green', 'No Impact')
    )
)
fig.update_traces(marker=dict(sizemode='diameter', sizemin=1, sizeref=1))

# Display in Streamlit with wide layout
st.title("College Football Weather Map")
if 'Timestamp' in df.columns:
    timestamp_str = df['Timestamp'].iloc[0]  # Get the timestamp string from the first row
    timestamp = datetime.fromisoformat(timestamp_str)
    formatted_timestamp = timestamp.strftime("%Y-%m-%d at %I:%M %p EST")
    st.subheader(f"Last updated: {formatted_timestamp}")
else:
    st.subheader("Timestamp not available")
st.plotly_chart(fig)
if st.sidebar.checkbox("Show game details", False):
    game = st.sidebar.selectbox("Select a game", df['Game'].unique())
    selected_game = df[df['Game'] == game]
    if not selected_game.empty:
        st.write(f"Details for {game}")
        
        selected_game = selected_game.rename(columns={
            'home_temp': 'Home_t', 
            'away_temp': 'Away_t',
            'away_fg': 'Away tm',
            'game_loc': 'Game Location',
            'Fd_open': 'Open',
            'FD_now': 'Current',
            'Open': 'Open_s',
            'Current': 'Current_s',
            'wind_fg': 'Wind',
            'temp_fg': 'Temp',
            'rain_fg': 'Rain',
            'wind_vol': 'Volatility',
            'wind_diff': 'Relative Wind',
            'year_built': 'Year',
            'wind_dir_fg': 'Wind_dir',
            'orient': 'Orient',
            'wind_impact': 'Wind_imp',
            'weakest_wind_effect': 'Weakest_dir'
        })
        
        columns_to_format = ['Away tm', 'Home_t', 'Away_t', 'Open', 'Current', 'Wind', 'My_total', 'Open_s', 'Current_s','Temp','Rain','Relative Wind']
        for col in columns_to_format:
            if col in ['Home_t', 'Away_t','Temp']:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}°")
            elif col == 'Away tm':
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}%")
            else:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}")
        
        selected_game['Impact'] = selected_game['gs_fg'].apply(lambda x: f"{x:.1f}%")
        
        selected_game['Year'] = selected_game['Year'].astype(int).astype(str)
        
        weather_columns = ['Wind', 'Temp', 'Rain', 'Impact', 'Volatility', 'Relative Wind', 'Home_t', 'Away_t', 'Year']
        odds_columns = ['Open', 'Current', 'My_total', 'Edge', 'Open_s', 'Current_s', 'Away tm']
        game_info_columns = ['Date', 'Time','Orient','Wind_dir','Wind_imp','Weakest_dir', 'Game Location']
        
        col1, col2 = st.columns(2)
        
        with col1:
            st.subheader("Weather Information")
            st.table(selected_game[weather_columns])
            
            st.subheader("Odds Information")
            st.table(selected_game[odds_columns])
            
            st.subheader("Game Information")
            st.table(selected_game[game_info_columns])
