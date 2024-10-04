import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

def load_combined_signals():
    # Load both datasets
    nfl_df = pd.read_csv('nfl_weather.csv')
    cfb_df = pd.read_excel('cfb_weather.xlsx', engine='openpyxl')
    
    # Add a league identifier column to each dataframe
    nfl_df['league'] = 'NFL'
    cfb_df['league'] = 'CFB'
    
    # Process coordinates for both datasets
    for df in [nfl_df, cfb_df]:
        df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
        df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
        df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
    
    # Filter for signals
    cfb_signals = cfb_df[
        (cfb_df['Open'].abs() < 10.5) & 
        (cfb_df['temp_fg'] < 70) & 
        (cfb_df['wind_fg'] > 15)
    ].copy()
    
    nfl_signals = nfl_df[
        (nfl_df['wind_fg'] > 15) & 
        (nfl_df['temp_fg'] < 60)
    ].copy()
    
    # Add signal type
    cfb_signals['signal_type'] = 'CFB Wind'
    nfl_signals['signal_type'] = 'NFL Wind'
    
    # Combine the filtered datasets
    combined_signals = pd.concat([cfb_signals, nfl_signals], ignore_index=True)
    
    # Process dot size and opacity
    combined_signals['dot_size'] = combined_signals['gs_fg'].abs()*4+7
    
    # Function to assign opacity based on wind impact
    def assign_dot_opacity(row):
        wind_impact = str(row['wind_impact']).lower()
        if wind_impact == 'high':
            return 1.0
        elif wind_impact == 'low':
            return 0.15
        elif wind_impact == 'med':
            return 0.5
        else:
            return 1.0

    combined_signals['dot_opacity'] = combined_signals.apply(assign_dot_opacity, axis=1)
    
    return combined_signals

def create_combined_signals_map():
    st.title("Combined Signals Weather Map")
    
    # Load and process the data
    df = load_combined_signals()
    
    # Create the map
    fig = px.scatter_mapbox(
        df,
        lat="lat",
        lon="lon",
        hover_name="Game",
        hover_data={
            "signal_type": True,
            "league": True,
            "wind_fg": True,
            "temp_fg": True,
            "game_loc": True,
            "wind_impact": True
        },
        size="dot_size",
        color="signal_type",
        color_discrete_map={
            'CFB Wind': 'purple',
            'NFL Wind': 'blue'
        },
        zoom=6,
        height=1000,
    )
    
    # Update layout
    fig.update_layout(
        mapbox_style="open-street-map",
        mapbox_center={"lat": 37.0902, "lon": -95.7129},
        mapbox_zoom=3.5,
        legend_title_text='Signal Types'
    )
    
    # Update hover template
    fig.update_traces(
        hovertemplate="<b>%{hovertext}</b><br>" + 
        "Signal: %{customdata[0]}<br>" +
        "League: %{customdata[1]}<br>" +
        "Wind: %{customdata[2]}<br>" +
        "Temperature: %{customdata[3]}°<br>" +
        "Location: %{customdata[4]}<br>" +
        "Wind Impact: %{customdata[5]}<extra></extra>"
    )
    
    # Apply opacity based on wind impact
    fig.update_traces(marker_opacity=df['dot_opacity'])
    
    # Display timestamp if available
    if 'Timestamp' in df.columns:
        timestamp_str = df['Timestamp'].iloc[0]
        timestamp = datetime.fromisoformat(timestamp_str)
        formatted_timestamp = timestamp.strftime("%Y-%m-%d at %I:%M %p EST")
        st.subheader(f"Last updated: {formatted_timestamp}")
    else:
        st.subheader("Timestamp not available")
    
    # Display the map
    st.plotly_chart(fig)
    
    # Add game details section
    if st.sidebar.checkbox("Show game details", False):
        game = st.sidebar.selectbox("Select a game", df['Game'].unique())
        selected_game = df[df['Game'] == game]
        
        if not selected_game.empty:
            st.write(f"Details for {game}")
            
            # Display relevant game information
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Game Information")
                info_df = selected_game[['league', 'signal_type', 'wind_fg', 'temp_fg', 'wind_impact', 'game_loc']].copy()
                info_df.columns = ['League', 'Signal Type', 'Wind', 'Temperature', 'Wind Impact', 'Location']
                st.table(info_df)

if __name__ == "__main__":
    create_combined_signals_map()
