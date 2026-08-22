const nativeFetch=window.fetch.bind(window);window.fetch=(url,options={})=>{const token=localStorage.getItem("auremgrid_session")||"";options.headers={...(options.headers||{}),Authorization:`Bearer ${token}`};return nativeFetch(url,options)};
const AGENCY_MODULES={Feedback:"Feedback",Performance:"Performance Insights",Forecasts:"Forecasts",Retention:"Retention"};
