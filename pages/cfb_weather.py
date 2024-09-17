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
df['Edge'] = (df['Edge'] * 100).round(2).astype(str) + '%'
df['My_total'] = (df['My_total'] * 4).round() / 4
# Assign dot color based on conditions
def assign_dot_color(row):
    if row['temp_fg'] > 80 and row['wind_fg'] < 12:
        return 'red'  # Heat
    elif row['temp_fg'] < 30 and row['wind_fg'] < 12:
        return 'lightblue'  # Cold
    elif row['wind_fg'] > 12:
        return 'purple'  # Wind
    elif row['rain_fg'] > 0 and row['wind_fg'] < 12:
        return 'yellow'  # Rain
    else:
        return 'green'  # Default/N/A

df['dot_color'] = df.apply(assign_dot_color, axis=1)

# Function to assign opacity, but only for purple dots (wind)
def assign_dot_opacity(row):
    if row['dot_color'] == 'purple':  # Only change opacity for 'Wind' dots
        if row['wind_vol'] == 'High':
            return 0.2  # Very low opacity for high wind
        elif row['wind_vol'] == 'Low':
            return 1.0  # Full opacity for low wind
        elif row['wind_vol'] == 'Mid':
            return 0.5  # Medium opacity for mid wind
        else:
            return 1.0  # Default opacity for undefined wind_vol
    else:
        return 1.0  # Full opacity for non-wind dots

df['dot_opacity'] = df.apply(assign_dot_opacity, axis=1)

# Create the map using Plotly
fig = px.scatter_mapbox(
    df,
    lat="lat",  # Use the 'lat' column
    lon="lon",  # Use the 'lon' column
    hover_name="Game",  # Column to show on hover
    hover_data={
        "wind_fg": True,   # Show wind forecast
        "temp_fg": True,   # Show temperature forecast
        "rain_fg": True,   # Show rain forecast
        "Fd_open": True,   # Show the opening FanDuel price
        "FD_now": True,    # Show the current FanDuel price
        "game_loc": True,  # Show game location
        "wind_diff": True, # Show wind difference
        "wind_vol": True,  # Show wind volatility
        "My_total": True,  # Add My_total to hover data
        "Edge": True,      # Add Edge to hover data
        "Open": True,      # Add Open spread to hover data
        "Current": True    # Add Current spread to hover data
    },
    size="dot_size",  # Use the 'gs_fg' field for dot size
    color="dot_color",  # Color based on conditions
    color_discrete_map={
        'red': 'red',
        'lightblue': 'lightblue',
        'purple': 'purple',
        'yellow': 'yellow',
        'green': 'green'
    },
    zoom=6,  # Adjusted for better zoom in the US
    height=1000,  # Make the map occupy a larger portion of the page
)

# Update the layout to focus on the US and adjust map display
fig.update_layout(
    mapbox_style="open-street-map",
    mapbox_center={"lat": 37.0902, "lon": -95.7129},  # Center the map in the U.S.
    mapbox_zoom=3.5,  # Zoom to focus on U.S. only
    legend_title_text='Weather Conditions',  # Set custom legend title
)

# Manually update the legend labels for the colors
fig.for_each_trace(
    lambda t: t.update(
        name=t.name.replace('red', 'Heat')
                   .replace('lightblue', 'Cold')
                   .replace('purple', 'Wind')
                   .replace('yellow', 'Rain')
                   .replace('green', 'N/A')
    )
)

# Apply opacity only to purple dots (Wind)
fig.update_traces(
    selector=dict(marker_color='purple'),  # Only select purple (wind) dots
    marker_opacity=df['dot_opacity']  # Apply opacity based on wind_vol
)
fig.update_traces(
    hovertemplate="<b>%{hovertext}</b><br>" + 
    "Wind: %{customdata[0]}<br>" +
    "Temp: %{customdata[1]}<br>" +
    "Rain: %{customdata[2]}<br>" +
    "FD Open: %{customdata[3]}<br>" +
    "FD Now: %{customdata[4]}<br>" +
    "Game Location: %{customdata[5]}<br>" +
    "Wind Diff: %{customdata[6]}<br>" +
    "Wind Volatility: %{customdata[7]}<br>" +
    "My Total: %{customdata[8]}<br>" +
    "Edge: %{customdata[9]}<br>" +
    "Open Spread: %{customdata[10]}<br>" +
    "Current Spread: %{customdata[11]}<extra></extra>"
)
# Display in Streamlit with wide layout
st.title("College Football Weather Map")
st.plotly_chart(fig, use_container_width=True)

# When a dot is clicked, show additional details
click_data = st.plotly_chart(fig, use_container_width=True).click_event

# If a dot is clicked, use the click data to show details for that game
if click_data:
    clicked_game = click_data['points'][0]['hovertext']  # 'hovertext' contains the 'Game' column

    st.write(f"Details for {clicked_game}")
    selected_game = df[df['Game'] == clicked_game]
    
    if not selected_game.empty:
        st.table(selected_game[['wind_fg', 'temp_fg', 'rain_fg', 'Fd_open', 'FD_now', 'game_loc', 'wind_diff', 'wind_vol', 'My_total', 'Edge', 'Open', 'Current']])
