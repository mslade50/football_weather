import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

def load_combined_signals():
    try:
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
            (cfb_df['wind_fg'] > 14)
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
        
        if len(combined_signals) == 0:
            st.warning("No games currently match the signal criteria.")
            return None
            
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
        
    except Exception as e:
        st.error(f"Error loading data: {str(e)}")
        return None

def create_combined_signals_map():
    st.title("Combined Signals Weather Map")
    
    # Load and process the data
    df = load_combined_signals()
    
    if df is None or len(df) == 0:
        st.write("No games currently match the signal criteria. Please check back later.")
        return
    
    # Create the map
    fig = px.scatter_mapbox(
        df,
        lat="lat",
        lon="lon",
        hover_name="Game",
        hover_data={
            "signal_type": True,
            "league": True,
            "Time": True,               # Corresponds to game_time
            "Date": True,               # Corresponds to game_date
            "wind_fg": True,           # Full game wind (you can use wind_fg or wind_avg)
            "temp_fg": True,            # Full game temperature
            "Open": True,            # Open spread
            "Current": True,             # Current spread
            "Fd_open": True,             # Open total
            "FD_now": True,         # Current total
            "wind_impact": True,
            "game_loc": True
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
        "Game Time: %{customdata[2]}<br>" +
        "Game Date: %{customdata[3]}<br>" +
        "Full Game Wind: %{customdata[4]} mph<br>" +
        "Full Game Temperature: %{customdata[5]:.1f}°F<br>" +
        "Open Spread: %{customdata[6]}<br>" +
        "Current Spread: %{customdata[7]}<br>" +
        "Open Total: %{customdata[8]}<br>" +
        "Current Total: %{customdata[9]}<br>" +
        "Wind Impact: %{customdata[10]}<br>" +
        "Location: %{customdata[11]}<extra></extra>"
    )
    
    # Apply opacity based on wind impact
    fig.update_traces(marker_opacity=df['dot_opacity'])
    
    # Display timestamp if available
    if 'Timestamp' in df.columns and len(df) > 0:
        try:
            timestamp_str = df['Timestamp'].iloc[0]
            timestamp = datetime.fromisoformat(timestamp_str)
            formatted_timestamp = timestamp.strftime("%Y-%m-%d at %I:%M %p EST")
            st.subheader(f"Last updated: {formatted_timestamp}")
        except:
            st.subheader("Timestamp not available")
    else:
        st.subheader("Timestamp not available")
    
    # Display the map
    st.plotly_chart(fig)
    
    # Add game details section
    if len(df) > 0 and st.sidebar.checkbox("Show game details", False):
        game = st.sidebar.selectbox("Select a game", df['Game'].unique())
        selected_game = df[df['Game'] == game]
        
        if not selected_game.empty:
            st.write(f"Details for {game}")
            
            # Display relevant game information
            col1, col2 = st.columns(2)
            
            with col1:
                st.subheader("Game Information")
                info_df = selected_game[['league', 'signal_type', 'wind_fg', 'temp_fg', 'wind_impact', 'game_loc', 'Time', 'Date', 'Fd_open', 'Spread', 'Odds_o', 'Total_Proj']].copy()
                info_df.columns = ['League', 'Signal Type', 'Wind', 'Temperature', 'Wind Impact', 'Location', 'Game Time', 'Game Date', 'Open Spread', 'Current Spread', 'Open Total', 'Current Total']
                st.table(info_df)

if __name__ == "__main__":
    create_combined_signals_map()
