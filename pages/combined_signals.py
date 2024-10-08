import streamlit as st
import pandas as pd
import plotly.express as px
from datetime import datetime

def load_combined_signals():
    try:
        # Load both datasets
        nfl_df = pd.read_csv('nfl_weather.csv')
        cfb_df = pd.read_excel('cfb_weather.xlsx', engine='openpyxl')
        nfl_df.rename(columns={
            'Total_open': 'Fd_open', 
            'Total_now': 'FD_now', 
            'Spread_open': 'Open', 
            'Spread_now': 'Current'
        }, inplace=True)
        
        # Add a league identifier column to each dataframe
        nfl_df['league'] = 'NFL'
        cfb_df['league'] = 'CFB'
        
        # Process coordinates for both datasets
        for df in [nfl_df, cfb_df]:
            df[['lat', 'lon']] = df['game_loc'].str.split(',', expand=True)
            df['lat'] = pd.to_numeric(df['lat'], errors='coerce')
            df['lon'] = pd.to_numeric(df['lon'], errors='coerce')
        
        # Filter for wind signals
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
        
        # Combine the filtered wind datasets
        combined_signals = pd.concat([cfb_signals, nfl_signals], ignore_index=True)

        # Filter for "Heat" signal: home and away temps < 57 and forecast temp_fg > 80
        heat_signals_cfb = cfb_df[
            (cfb_df['home_temp'] < 57) & 
            (cfb_df['away_temp'] < 57) & 
            (cfb_df['temp_fg'] > 80)
        ].copy()

        heat_signals_nfl = nfl_df[
            (nfl_df['home_temp'] < 57) & 
            (nfl_df['away_temp'] < 57) & 
            (nfl_df['temp_fg'] > 80)
        ].copy()
        
        # Add signal type for heat signals
        heat_signals_cfb['signal_type'] = 'CFB Heat'
        heat_signals_nfl['signal_type'] = 'NFL Heat'
        
        # Combine heat signals with existing wind signals
        combined_signals = pd.concat([combined_signals, heat_signals_cfb, heat_signals_nfl], ignore_index=True)

        # Add Alt+Heat signal for CFB where travel_alt > 800, opening spread is between -10 and 10, and temp_fg > 75
        alt_heat_cfb = cfb_df[
            (cfb_df['travel_alt'] > 800) &
            (cfb_df['Open'].between(-10, 10)) &
            (cfb_df['temp_fg'] > 75)
        ].copy()
        
        # Set signal type and color
        alt_heat_cfb['signal_type'] = 'Alt+Heat'
        
        # Add Alt+Heat signals to the combined dataset
        combined_signals = pd.concat([combined_signals, alt_heat_cfb], ignore_index=True)
        
        if len(combined_signals) == 0:
            st.warning("No games currently match the signal criteria.")
            return None
            
        # Process dot size and opacity
        combined_signals['dot_size'] = combined_signals['gs_fg'].abs()*4 + 7
        
        # Ensure that heat signals have full opacity (1.0)
        def assign_dot_opacity(row):
            if 'Heat' in row['signal_type']:
                return 1.0
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
            "Time": True,
            "Date": True,
            "wind_fg": True,
            "temp_fg": True,
            "Open": True,
            "Current": True,
            "Fd_open": True,
            "FD_now": True,
            "wind_impact": True,
            "game_loc": True
        },
        size="dot_size",
        color="signal_type",
        color_discrete_map={
            'CFB Wind': 'purple',
            'NFL Wind': 'blue',
            'CFB Heat': 'red',
            'NFL Heat': 'red',
            'Alt+Heat': 'saddlebrown'  # New color for Alt+Heat signal
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
                info_df = selected_game[['league', 'signal_type', 'wind_fg', 'temp_fg', 'wind_impact', 'game_loc', 'Time', 'Date', 'Open', 'Current', 'Fd_open', 'FD_now']].copy()
                info_df.columns = ['League', 'Signal Type', 'Wind', 'Temperature', 'Wind Impact', 'Location', 'Game Time', 'Game Date', 'Open Spread', 'Current Spread', 'Open Total', 'Current Total']
                st.table(info_df)

if __name__ == "__main__":
    create_combined_signals_map()
