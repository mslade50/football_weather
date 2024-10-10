import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime
from streamlit_plotly_events import plotly_events

st.set_page_config(layout="wide")

# Load your CSV file
df = pd.read_csv('nfl_weather.csv')
df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
df['gs_fg'] = df['gs_fg'] * 100
df['away_fg'] = df['away_fg'] * 100
df['wind_diff'] = df['wind_fg'] - df['avg_wind']

# Update 'wind_vol' to 'Low' if 'wind_fg' is less than 11.99
df.loc[df['wind_fg'] < 11.99, 'wind_vol'] = 'Low'

# Assign dot color and size based on new impact conditions
def assign_impact_and_color(row):
    if (row['rain_fg'] > 2) or (8 < row['wind_fg'] < 15 and row['temp_fg'] < 60):
        return 'Low Impact', 'blue', 15
    elif row['wind_fg'] > 15 and row['temp_fg'] < 60:
        return 'Mid Impact', 'orange', 25
    elif row['wind_fg'] > 15 and 32 <= row['temp_fg'] <= 45:
        return 'High Impact', 'purple', 40
    else:
        return 'No Impact', 'green', 7

df['impact_level'], df['dot_color'], df['dot_size'] = zip(*df.apply(assign_impact_and_color, axis=1))

# Function to assign opacity, but only for dots with purple color (high wind)
def assign_dot_opacity(row):
    if row['wind_impact'] == 'high':
        return 1.0  # Full opacity for high wind impact
    elif row['wind_impact'] == 'low':
        return 0.15  # Very low opacity for low wind impact
    elif row['wind_impact'] == 'med':
        return 0.5  # Medium opacity for medium wind impact
    else:
        return 1.0 

df['dot_opacity'] = df.apply(assign_dot_opacity, axis=1)

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
        "Total_open": True,
        "Total_now": True,
        "game_loc": True,
        "wind_vol": True,
        "Spread_open": True,
        "Spread_now": True
    },
    size="dot_size",
    color="dot_color",
    color_discrete_map={
        'blue': 'blue',
        'orange': 'orange',
        'purple': 'purple',
        'green': 'green'
    },
    zoom=6,
    height=1000,
)

# Update layout to focus on the US and set legend
fig.update_layout(
    mapbox_style="open-street-map",
    mapbox_center={"lat": 37.0902, "lon": -95.7129},
    mapbox_zoom=3.5,
    legend_title_text='Weather Conditions'
)

# Manually update the legend labels for the colors
fig.for_each_trace(
    lambda t: t.update(
        name=t.name.replace('blue', 'Low Impact')
                   .replace('orange', 'Mid Impact')
                   .replace('purple', 'High Impact')
                   .replace('green', 'No Impact')
    )
)
fig.update_traces(marker=dict(sizemode='diameter', sizemin=1, sizeref=1))

# Apply opacity based on the wind impact level for purple dots (Wind)
fig.update_traces(
    selector=dict(marker_color='purple'),  # Only select purple (high impact) dots
    marker_opacity=df['dot_opacity']
)

# Update hover template to keep the current configuration
fig.update_traces(
    hovertemplate="<b>%{hovertext}</b><br>" + 
    "Wind: %{customdata[0]}<br>" +
    "Temp: %{customdata[1]}<br>" +
    "Rain: %{customdata[2]}<br>" +
    "Weather Impact: %{customdata[3]}%<br>" +
    "Total (Open): %{customdata[4]}<br>" +
    "Total (Now): %{customdata[5]}<br>" +
    "Game Location: %{customdata[6]}<br>" +
    "Wind Volatility: %{customdata[7]}<br>" +
    "Spread (Open): %{customdata[8]}<br>" +
    "Spread (Now): %{customdata[9]}<extra></extra>"
)

# Display in Streamlit with wide layout
st.title("NFL Weather Map")
if 'Timestamp' in df.columns:
    timestamp_str = df['Timestamp'].iloc[0]
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

        # Rename columns for display
        selected_game = selected_game.rename(columns={
            'home_temp': 'Home_t', 
            'away_temp': 'Away_t',
            'away_fg': 'Away tm',
            'game_loc': 'Game Location',
            'Total_open': 'Open',
            'Total_now': 'Current',
            'Under_open': 'Price',
            'Under_now': 'Price Now',
            'Spread_open': 'Open_s',
            'Spread_now': 'Current_s',
            'wind_fg': 'Wind',
            'temp_fg': 'Temp',
            'rain_fg': 'Rain',
            'wind_vol': 'Volatility',
            'wind_diff': 'Relative Wind',
            'year_built': 'Year',
            'wind_dir_fg': 'Wind_dir',
            'orient': 'Orientation',
            'wind_impact': 'Wind Impact',
            'weakest_wind_effect': 'Weakest Wind'
        })

        columns_to_format = ['Away tm', 'Home_t', 'Away_t', 'Open', 'Current', 'Wind', 'Open_s', 'Current_s', 'Temp', 'Rain', 'Relative Wind']
        for col in columns_to_format:
            if col in ['Home_t', 'Away_t', 'Temp']:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}°")
            elif col == 'Away tm':
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}%")
            else:
                selected_game[col] = selected_game[col].apply(lambda x: f"{x:.1f}")

        selected_game['Impact'] = selected_game['gs_fg'].apply(lambda x: f"{x:.1f}%")
        selected_game['Year'] = selected_game['Year'].astype(int).astype(str)

        weather_columns = ['Wind', 'Temp', 'Rain', 'Impact', 'Volatility', 'Relative Wind', 'Home_t', 'Away_t', 'Year']
        odds_columns = ['Open', 'Price', 'Current', 'Price Now', 'Open_s', 'Current_s', 'Away tm']
        game_info_columns = ['Orientation', 'Wind Impact', 'Wind_dir', 'Weakest Wind', 'Date', 'Time', 'Game Location']

        col1, col2 = st.columns(2)

        with col1:
            st.subheader("Weather Information")
            st.table(selected_game[weather_columns].reset_index(drop=True))

            st.subheader("Odds Information")
            st.table(selected_game[odds_columns].reset_index(drop=True))

            st.subheader("Game Information")
            st.table(selected_game[game_info_columns].reset_index(drop=True))
