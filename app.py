import streamlit as st
import pandas as pd
import plotly.express as px
import os
from io import BytesIO
from datetime import datetime

# --- PAGE CONFIGURATION ---
st.set_page_config(
    page_title="RTM Prepicking Operations",
    page_icon="⚡",
    layout="wide",
    initial_sidebar_state="expanded"
)

# Custom CSS for KPI cards
st.markdown("""
    <style>
    div[data-testid="metric-container"] {
        background-color: #f8f9fa;
        border: 1px solid #e0e0e0;
        border-radius: 8px;
        padding: 15px;
        box-shadow: 2px 2px 5px rgba(0,0,0,0.05);
    }
    </style>
""", unsafe_allow_html=True)

st.title("⚡ RTM Prepicking Operations Dashboard")
st.caption(f"Last Refreshed: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}")

# --- SIDEBAR: DATA INPUT ---
st.sidebar.header("📁 Data Sources")

rtm_file = st.sidebar.file_uploader("1. Upload RTM Dashboard (CSV)", type=['csv'])
soh_file = st.sidebar.file_uploader("2. Upload DC SOH Export (CSV)", type=['csv'])
tcs_file = st.sidebar.file_uploader("3. Upload TCS Schedule (Excel)", type=['xlsx', 'xls'])

@st.cache_data
def load_and_merge_data(rtm_input, soh_input, tcs_input):
    # 1. Load Data with Local Fallbacks
    if rtm_input is not None: rtm_df = pd.read_csv(rtm_input)
    elif os.path.exists('rtm.csv'): rtm_df = pd.read_csv('rtm.csv')
    else: return None, None, None

    if soh_input is not None: soh_df = pd.read_csv(soh_input)
    elif os.path.exists('soh.csv'): soh_df = pd.read_csv('soh.csv')
    else: return None, None, None
    
    tcs_df = None
    if tcs_input is not None: tcs_df = pd.read_excel(tcs_input)
    elif os.path.exists('tcs.xlsx'): tcs_df = pd.read_excel('tcs.xlsx')

    # 2. Standardize Keys
    rtm_df['LPN'] = rtm_df['OBLPN'].astype(str).str.strip().str.upper()
    soh_df['LPN'] = soh_df['LPB Nbr'].astype(str).str.strip().str.upper()
    rtm_df['Seller_Name_Clean'] = rtm_df['Seller Name'].fillna(rtm_df['Supplier Name'])
    rtm_df['Booking_Date_Parsed'] = pd.to_datetime(rtm_df['Booking Date'], errors='coerce')
    
    # Safe cast Seller IDs to strings to ensure matching
    soh_df['Seller ID'] = soh_df['Seller ID'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()
    if tcs_df is not None:
        tcs_df['Seller ID'] = tcs_df['Seller ID'].astype(str).str.replace(r'\.0$', '', regex=True).str.strip()

    # 3. Merge RTM & SOH
    rtm_clean = rtm_df[['Facility', 'Courier Flag', 'Booking Date', 'Booking_Date_Parsed', 'Booking Nbr', 'LPN', 'OBI', 'Seller_Name_Clean', 'Pack QTY', 'Current Location']]
    soh_dedup = soh_df[['LPN', 'Location Area', 'Location Barcode', 'Qty', 'LPN Status', 'Desc']].drop_duplicates(subset=['LPN'])

    merged_df = pd.merge(rtm_clean, soh_dedup, on='LPN', how='left')

    picked_areas = ['MCSS', 'MCSP', 'MCSR', 'CCSP', 'MCSD', 'SC', 'CC', 'MCSF']
    
    def classify_status(row):
        loc_area = str(row.get('Location Area', '')).strip().upper()
        if pd.isna(row.get('Location Area')): return 'Missing from SOH'
        elif loc_area in picked_areas: return 'Picked'
        else: return 'Outstanding'

    merged_df['Prepick_Status'] = merged_df.apply(classify_status, axis=1)
    
    def extract_zone(row):
        barcode = str(row.get('Location Barcode', '')).strip().upper()
        if row.get('Location Area') == 'RTCR' and barcode.startswith('RTCR') and len(barcode) >= 6:
            return barcode[4:6]
        return 'N/A'
        
    merged_df['Location Zone'] = merged_df.apply(extract_zone, axis=1)
    merged_df['Courier Flag'] = merged_df['Courier Flag'].fillna('Unassigned')
    
    return merged_df, soh_df, tcs_df

merged_data, soh_raw_df, tcs_raw_df = load_and_merge_data(rtm_file, soh_file, tcs_file)

if merged_data is None:
    st.info("👈 **Please upload your RTM & SOH CSV files in the sidebar to begin.**")
    st.stop()

# --- SIDEBAR: FILTERS & HORIZON ---
st.sidebar.markdown("---")
st.sidebar.header("🗓️ Time Horizon")

sorted_dates_df = merged_data[['Booking Date', 'Booking_Date_Parsed']].dropna().drop_duplicates().sort_values('Booking_Date_Parsed')
unique_booking_dates = sorted_dates_df['Booking Date'].tolist()

horizon_option = st.sidebar.selectbox("Day Horizon", ["1 Day (Next Shift)", "2 Days", "3 Days", "5 Days", "7 Days", "All Dates", "Custom Select"], index=0)

selected_dates = []
if horizon_option == "1 Day (Next Shift)": selected_dates = unique_booking_dates[:1]
elif horizon_option == "2 Days": selected_dates = unique_booking_dates[:2]
elif horizon_option == "3 Days": selected_dates = unique_booking_dates[:3]
elif horizon_option == "5 Days": selected_dates = unique_booking_dates[:5]
elif horizon_option == "7 Days": selected_dates = unique_booking_dates[:7]
elif horizon_option == "All Dates": selected_dates = unique_booking_dates
elif horizon_option == "Custom Select":
    selected_dates = st.sidebar.multiselect("Select Dates:", options=unique_booking_dates, default=unique_booking_dates[:1] if unique_booking_dates else [])

# --- DYNAMIC TFS INJECTION BASED ON HORIZON ---
filtered_df = merged_data.copy()

# Add TFS scheduled pickups to the workload if TCS is uploaded
if tcs_raw_df is not None and len(selected_dates) > 0:
    tfs_records_list = []
    
    for date_str in selected_dates:
        date_obj = pd.to_datetime(date_str, errors='coerce')
        if pd.isna(date_obj): continue
        day_of_week = date_obj.strftime('%A')
        
        jhb_sellers, cpt_sellers = [], []
        
        # Pull matching sellers for this day from TCS schedule
        if 'JHB' in tcs_raw_df.columns:
            jhb_sellers = tcs_raw_df[tcs_raw_df['JHB'].astype(str).str.strip().str.title() == day_of_week]['Seller ID'].unique()
        if 'CPT' in tcs_raw_df.columns:
            cpt_sellers = tcs_raw_df[tcs_raw_df['CPT'].astype(str).str.strip().str.title() == day_of_week]['Seller ID'].unique()
            
        # Get live stock from SOH for these sellers
        tfs_jhb_soh = soh_raw_df[(soh_raw_df['Facility'] == 'JHB') & (soh_raw_df['Seller ID'].isin(jhb_sellers))]
        tfs_cpt_soh = soh_raw_df[(soh_raw_df['Facility'] == 'CPT') & (soh_raw_df['Seller ID'].isin(cpt_sellers))]
        tfs_soh = pd.concat([tfs_jhb_soh, tfs_cpt_soh])
        
        if not tfs_soh.empty:
            tfs_records = pd.DataFrame({
                'Facility': tfs_soh['Facility'],
                'Courier Flag': 'TFS Scheduled',
                'Booking Date': date_str,
                'Booking_Date_Parsed': date_obj,
                'Booking Nbr': 'TFS_' + tfs_soh['Seller ID'].astype(str),
                'LPN': tfs_soh['LPB Nbr'].astype(str).str.strip().str.upper(),
                'OBI': tfs_soh['OBI'],
                'Seller_Name_Clean': tfs_soh['Seller Name'],
                'Pack QTY': tfs_soh['Qty'],
                'Current Location': tfs_soh['Location Barcode'],
                'Location Area': tfs_soh['Location Area'],
                'Location Barcode': tfs_soh['Location Barcode'],
                'Qty': tfs_soh['Qty'],
                'LPN Status': tfs_soh['LPN Status'],
                'Desc': tfs_soh['Desc']
            })
            tfs_records_list.append(tfs_records)
            
    if len(tfs_records_list) > 0:
        all_tfs = pd.concat(tfs_records_list, ignore_index=True)
        
        # De-duplicate: If LPN is already requested in RTM, drop it from TFS
        all_tfs = all_tfs[~all_tfs['LPN'].isin(filtered_df['LPN'])]
        
        # Apply Status Logic
        picked_areas = ['MCSS', 'MCSP', 'MCSR', 'CCSP', 'MCSD', 'SC', 'CC', 'MCSF']
        all_tfs['Prepick_Status'] = all_tfs['Location Area'].apply(
            lambda x: 'Missing from SOH' if pd.isna(x) else ('Picked' if str(x).strip().upper() in picked_areas else 'Outstanding')
        )
        
        def extract_zone(row):
            barcode = str(row.get('Location Barcode', '')).strip().upper()
            if row.get('Location Area') == 'RTCR' and barcode.startswith('RTCR') and len(barcode) >= 6:
                return barcode[4:6]
            return 'N/A'
            
        all_tfs['Location Zone'] = all_tfs.apply(extract_zone, axis=1)
        
        # Combine RTM and generated TFS stock list
        filtered_df = pd.concat([filtered_df, all_tfs], ignore_index=True)

# --- ADDITIONAL FILTERS ---
st.sidebar.markdown("---")
st.sidebar.header("🎯 Operational Filters")

available_facilities = sorted(filtered_df['Facility'].dropna().unique())
selected_facilities = st.sidebar.multiselect("Warehouse Facility", options=available_facilities, default=available_facilities[:1] if 'JHB' in available_facilities else available_facilities)

available_couriers = sorted(filtered_df['Courier Flag'].dropna().unique())
selected_couriers = st.sidebar.multiselect("Courier / Dispatch Truck", options=available_couriers, placeholder="All Couriers (Leave blank)")

search_query = st.sidebar.text_input("Search Booking Nbr or LPN", placeholder="e.g. TALB...")

# Filter Application
if selected_facilities: filtered_df = filtered_df[filtered_df['Facility'].isin(selected_facilities)]
if selected_couriers: filtered_df = filtered_df[filtered_df['Courier Flag'].isin(selected_couriers)]
if selected_dates: filtered_df = filtered_df[filtered_df['Booking Date'].isin(selected_dates)]
if search_query:
    q = search_query.strip().upper()
    filtered_df = filtered_df[filtered_df['Booking Nbr'].astype(str).str.upper().str.contains(q) | filtered_df['LPN'].astype(str).str.upper().str.contains(q)]


# --- INTELLIGENT ALERTS ---
total_lpns = len(filtered_df)
picked_count = (filtered_df['Prepick_Status'] == 'Picked').sum()
outstanding_count = (filtered_df['Prepick_Status'] == 'Outstanding').sum()
missing_count = (filtered_df['Prepick_Status'] == 'Missing from SOH').sum()
completion_rate = (picked_count / total_lpns * 100) if total_lpns > 0 else 0.0

if horizon_option == "1 Day (Next Shift)" and completion_rate < 80.0 and total_lpns > 0:
    st.warning(f"⚠️ **Shift Alert:** The next shift is only **{completion_rate:.1f}%** picked. Check the Generated Worklist tab to fast-track picks.")
if missing_count > 0:
    st.error(f"🚨 **Critical Exception:** There are **{missing_count} LPNs** missing from the physical stock SOH. Check the 'Missing from SOH' tab.")

# --- KPI METRICS ---
col1, col2, col3, col4, col5 = st.columns(5)
col1.metric("Total Required LPNs", f"{total_lpns:,}")
col2.metric("Picked / Marshalled", f"{picked_count:,}", delta=f"{completion_rate:.1f}% Ready")
col3.metric("Outstanding to Pick", f"{outstanding_count:,}")
col4.metric("Missing SOH Exceptions", f"{missing_count:,}", delta_color="inverse")
col5.metric("Overall Shift Completion", f"{completion_rate:.1f}%")

st.divider()

# --- VISUALIZATIONS ---
chart_col1, chart_col2 = st.columns([1, 2])

with chart_col1:
    st.markdown("#### Operational Status")
    status_counts = filtered_df['Prepick_Status'].value_counts().reset_index()
    status_counts.columns = ['Status', 'Count']
    fig_pie = px.pie(status_counts, values='Count', names='Status', color='Status',
        color_discrete_map={'Picked': '#2ecc71', 'Outstanding': '#3498db', 'Missing from SOH': '#e74c3c'}, hole=0.4)
    fig_pie.update_layout(margin=dict(t=20, b=20, l=10, r=10), height=300, showlegend=True)
    st.plotly_chart(fig_pie, use_container_width=True)

with chart_col2:
    st.markdown("#### Pick Progress by Courier / Dispatch")
    courier_chart_data = filtered_df.groupby('Courier Flag').agg(
        Picked=('Prepick_Status', lambda x: (x == 'Picked').sum()),
        Outstanding=('Prepick_Status', lambda x: (x == 'Outstanding').sum())
    ).reset_index().sort_values(by='Outstanding', ascending=False)
    
    fig_bar = px.bar(courier_chart_data, x='Courier Flag', y=['Picked', 'Outstanding'],
        color_discrete_map={'Picked': '#2ecc71', 'Outstanding': '#3498db'}, barmode='stack',
        labels={'value': 'LPN Count', 'variable': 'Status'})
    fig_bar.update_layout(margin=dict(t=20, b=20, l=10, r=10), height=300, xaxis_title=None)
    st.plotly_chart(fig_bar, use_container_width=True)

st.divider()

# --- DATA TABS ---
tab1, tab2, tab3, tab4, tab5 = st.tabs([
    "📋 Booking & TFS Summary", 
    "📍 Physical Workload", 
    "🖨️ Generated Prepick Worklist", 
    "🔍 Full Detail", 
    "⚠️ Missing SOH"
])

# TAB 1: BOOKING SUMMARY
with tab1:
    st.markdown("#### High-Level Booking Status")
    summary_df = filtered_df.groupby(['Booking Date', 'Booking Nbr', 'Courier Flag', 'Seller_Name_Clean']).agg(
        Total_LPNs=('LPN', 'count'),
        Picked=('Prepick_Status', lambda x: (x == 'Picked').sum()),
        Outstanding=('Prepick_Status', lambda x: (x == 'Outstanding').sum())
    ).reset_index().rename(columns={'Seller_Name_Clean': 'Seller / Supplier'})

    summary_df['% Picked'] = (summary_df['Picked'] / summary_df['Total_LPNs'] * 100).round(1)
    summary_df = summary_df.sort_values(by=['% Picked', 'Total_LPNs'], ascending=[True, False])

    st.dataframe(
        summary_df,
        column_config={"% Picked": st.column_config.ProgressColumn("% Picked", format="%.1f%%", min_value=0, max_value=100)},
        use_container_width=True, hide_index=True
    )

# TAB 2: LOCATION WORKLOAD
with tab2:
    col_loc1, col_loc2 = st.columns(2)
    
    with col_loc1:
        st.markdown("#### 1. Warehouse Location Breakdown")
        loc_df = filtered_df.groupby('Location Area', dropna=False).agg(
            Total_LPNs=('LPN', 'count'),
            Picked=('Prepick_Status', lambda x: (x == 'Picked').sum()),
            Outstanding=('Prepick_Status', lambda x: (x == 'Outstanding').sum()),
            Distinct_Bins=('Location Barcode', 'nunique')
        ).reset_index().fillna({'Location Area': 'Missing from SOH'}).sort_values(by='Total_LPNs', ascending=False)
        st.dataframe(loc_df, use_container_width=True, hide_index=True)

    with col_loc2:
        st.markdown("#### 2. Distinct Physical Locations per Area")
        distinct_loc_df = filtered_df.groupby('Location Area', dropna=False).agg(
            Distinct_Locations=('Location Barcode', 'nunique'), Total_LPNs=('LPN', 'count')
        ).reset_index().fillna({'Location Area': 'Missing from SOH'}).sort_values(by='Distinct_Locations', ascending=False)
        st.dataframe(distinct_loc_df, use_container_width=True, hide_index=True)

    st.divider()
    col_res1, col_res2 = st.columns(2)

    with col_res1:
        st.markdown("#### 3. Reserve Pick Workload (RTCM, RTCR, RTCF)")
        reserve_areas = ['RTCM', 'RTCR', 'RTCF']
        reserve_outstanding = filtered_df[(filtered_df['Prepick_Status'] == 'Outstanding') & (filtered_df['Location Area'].isin(reserve_areas))]
        reserve_summary = reserve_outstanding.groupby('Location Area').agg(
            Outstanding_LPNs=('LPN', 'count'), Unique_Bins_To_Visit=('Location Barcode', 'nunique')
        ).reset_index()

        for area in reserve_areas:
            if area not in reserve_summary['Location Area'].values:
                reserve_summary = pd.concat([reserve_summary, pd.DataFrame([{'Location Area': area, 'Outstanding_LPNs': 0, 'Unique_Bins_To_Visit': 0}])], ignore_index=True)

        st.dataframe(reserve_summary.sort_values(by='Outstanding_LPNs', ascending=False), use_container_width=True, hide_index=True)

    with col_res2:
        st.markdown("#### 4. RTCR Outstanding by Zone")
        rtcr_out = filtered_df[(filtered_df['Prepick_Status'] == 'Outstanding') & (filtered_df['Location Area'] == 'RTCR')]
        if not rtcr_out.empty:
            zone_sum = rtcr_out.groupby('Location Zone').agg(
                Outstanding_LPNs=('LPN', 'count'), Distinct_Bins=('Location Barcode', 'nunique')
            ).reset_index().sort_values(by='Distinct_Bins', ascending=False)
            st.dataframe(zone_sum, use_container_width=True, hide_index=True)
        else:
            st.success("✅ No outstanding RTCR stock!")

# TAB 3: GENERATED PREPICK WORKLIST
with tab3:
    st.markdown("#### 🖨️ Master Prepick Action List")
    st.caption("Sorted intelligently by Location Area and Barcode to create an efficient walking path for pickers.")
    
    worklist_df = filtered_df[
        (filtered_df['Prepick_Status'] == 'Outstanding') & 
        (filtered_df['Location Barcode'].notna())
    ].copy()
    
    if len(worklist_df) > 0:
        worklist_sorted = worklist_df.sort_values(by=['Location Area', 'Location Barcode', 'Booking Date'])
        
        display_worklist = worklist_sorted[[
            'Location Area', 'Location Barcode', 'LPN', 'Desc', 'Pack QTY', 'Booking Date', 'Booking Nbr', 'Courier Flag'
        ]].rename(columns={'Desc': 'Item Description'})
        
        st.dataframe(display_worklist, use_container_width=True, hide_index=True)
    else:
        st.success("🎉 All selected tasks have been picked. Worklist is empty!")
        display_worklist = pd.DataFrame()

# TAB 4: DETAIL LIST
with tab4:
    st.markdown("#### Individual LPN Detail List")
    st.dataframe(filtered_df[['Facility', 'Courier Flag', 'Booking Date', 'Booking Nbr', 'Seller_Name_Clean', 'OBI', 'LPN', 'Prepick_Status', 'Location Area', 'Location Zone', 'Location Barcode', 'Pack QTY']], use_container_width=True, hide_index=True)

# TAB 5: EXCEPTIONS
with tab5:
    st.markdown("#### Missing LPN Action List")
    missing_df = filtered_df[filtered_df['Prepick_Status'] == 'Missing from SOH']
    if len(missing_df) > 0:
        st.dataframe(missing_df[['Facility', 'Courier Flag', 'Booking Date', 'Booking Nbr', 'Seller_Name_Clean', 'OBI', 'LPN', 'Pack QTY', 'Current Location']], use_container_width=True, hide_index=True)
    else:
        st.success("🎉 No missing LPNs found!")

# --- EXPORT REPORT ---
st.sidebar.markdown("---")
st.sidebar.header("📥 Export & Share")

def get_excel():
    output = BytesIO()
    with pd.ExcelWriter(output, engine='openpyxl') as writer:
        summary_df.to_excel(writer, sheet_name='Booking Summary', index=False)
        loc_df.to_excel(writer, sheet_name='Location Breakdown', index=False)
        if len(display_worklist) > 0:
            display_worklist.to_excel(writer, sheet_name='Generated Picklist', index=False)
        filtered_df.to_excel(writer, sheet_name='LPN Detail Status', index=False)
    return output.getvalue()

st.sidebar.download_button(
    label="📊 Download Full Operations Report (Excel)", 
    data=get_excel(), 
    file_name=f"Prepicking_Export_{datetime.now().strftime('%H%M')}.xlsx", 
    mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
)

if len(display_worklist) > 0:
    st.sidebar.download_button(
        label="🖨️ Download Prepick Worklist (CSV)",
        data=display_worklist.to_csv(index=False).encode('utf-8'),
        file_name=f"Walking_Path_Picklist_{datetime.now().strftime('%H%M')}.csv",
        mime="text/csv",
        type="primary"
    )
