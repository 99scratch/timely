import TimelyMetric
from TimeDateRange import TimeDateRange
from TimeDateRange import UTC
from TimelyAnalyticConfiguration import TimelyAnalyticConfiguration
import DataOperations
import pandas
import re
from datetime import datetime
from datetime import timedelta

from TimelyAlert import TimelyAlert
import numpy as np

utc = UTC()

def logTiming(timelyMetric, startTime, stopTime):
    df = timelyMetric.getDataFrame()
    print("start: " + str(startTime))
    print("stop: " + str(stopTime))
    print("num records: " + str(len(df)))
    elapsedSec = (TimeDateRange.unix_time_millis(stopTime) - TimeDateRange.unix_time_millis(startTime)) / 1000
    print("elapsed seconds: " + str(elapsedSec))
    recPerSec = len(df) / elapsedSec
    print("records per second: " + recPerSec)

def addCondition(orCondition, currResult, condition):
    if orCondition:
        currResult = currResult | condition
    else:
        currResult = currResult & condition
    return currResult

def keepConsecutiveAlerts(dataFrame, exceptions, minimumSpan):

    if exceptions.size > 0 and minimumSpan is not None:

        currentFirst = None
        currentLast = None
        result_span_values = pandas.DataFrame()
        result_span_values['bool'] = np.zeros(exceptions.shape, bool)
        result_span_values = result_span_values.set_index(exceptions.index)
        current_result_span_values = pandas.DataFrame()
        current_result_span_values['bool'] = np.zeros(exceptions.shape, bool)
        current_result_span_values = current_result_span_values.set_index(exceptions.index)

        for index, row in dataFrame.iterrows():
            if index in exceptions.index:
                current_result_span_values.loc[index]['bool'] = True
                currentLast = index
                if currentFirst is None:
                    currentFirst = index
                if int((currentLast - currentFirst).total_seconds() / 60) >= int(minimumSpan):
                    result_span_values['bool'] = result_span_values['bool'] | current_result_span_values['bool']
            else:
                current_result_span_values['bool'] = np.zeros(exceptions.shape, bool)
                currentFirst = None
                currentLast = None

        return exceptions.loc[result_span_values['bool']]
    else:
        return exceptions

def convertCounterToRate(timelyMetric, analyticConfig):
    # Adjust counter metric into a rate metric
    df = timelyMetric.getDataFrame()
    columnNames = df[analyticConfig.groupByColumn].unique()
    df = DataOperations.pivot(df, timelyMetric.metric, analyticConfig.groupByColumn)
    df = DataOperations.resample(df, analyticConfig.sample, how=analyticConfig.how)
    for c in columnNames:
        df[c] = df[c].diff(1) / (analyticConfig.sample_minutes * 60 * 1000)
    df = DataOperations.unpivot(df, timelyMetric.metric, analyticConfig.groupByColumn)
    df = df.dropna()
    timelyMetric.setDataFrame(df)

def find_alerts(timelyMetric, analyticConfig, notebook=False):

    if (analyticConfig.counter == True):
        convertCounterToRate(timelyMetric, analyticConfig)

    df = timelyMetric.getDataFrame()

    graphDF = TimelyMetric.pivot(df, timelyMetric.metric, groupByColumn=analyticConfig.groupByColumn)

    if analyticConfig.excludeColRegex is not None:
        graphDF = graphDF.select(lambda x : not (re.search(analyticConfig.excludeColRegex, x)), axis=1)
    if analyticConfig.includeColRegex is not None:
        graphDF = graphDF.select(lambda x : re.search(analyticConfig.includeColRegex, x), axis=1)

    if analyticConfig.sample is not None:
        graphDF = TimelyMetric.resample(graphDF, analyticConfig.sample, how=analyticConfig.how, interpolate=analyticConfig.interpolate, fill=analyticConfig.fill)

    graphDF_avg = pandas.DataFrame(graphDF, copy=True)

    combined = pandas.DataFrame()

    seriesConfig = {}
    for i in graphDF_avg.columns:
        col = str(i)

        any_conditions_values = False
        result_values = np.ones(graphDF[col].shape, bool)
        if analyticConfig.orCondition:
            result_values = np.zeros(graphDF[col].shape, bool)

        any_conditions_average = False
        result_average = np.ones(graphDF_avg[col].shape, bool)
        if analyticConfig.orCondition:
            result_average = np.zeros(graphDF_avg[col].shape, bool)

        if analyticConfig.min_threshold is not None:
            currCondition = graphDF[col].astype(float) < analyticConfig.min_threshold
            result_values = addCondition(analyticConfig.orCondition, result_values, currCondition)
            any_conditions_values = True

        if analyticConfig.max_threshold is not None:
            currCondition = graphDF[col].astype(float) > analyticConfig.max_threshold
            result_values = addCondition(analyticConfig.orCondition, result_values, currCondition)
            any_conditions_values = True

        if analyticConfig.rolling_average_samples is not None:
            graphDF_avg = TimelyMetric.rolling_average(graphDF_avg, col, rolling_average=analyticConfig.rolling_average_samples)
            if analyticConfig.min_threshold_percentage is not None:
                if analyticConfig.min_threshold_percentage >= 0:
                    multiple = 1.0 + (float(abs(analyticConfig.min_threshold_percentage)) / float(100))
                else:
                    multiple = 1.0 - (float(abs(analyticConfig.min_threshold_percentage)) / float(100))
                currCondition = graphDF[col].astype(float) < (graphDF_avg[col].astype(float) * multiple)
                result_values = addCondition(analyticConfig.orCondition, result_values, currCondition)
                any_conditions_values = True

            if analyticConfig.max_threshold_percentage is not None:
                if analyticConfig.max_threshold_percentage >= 0:
                    multiple = 1.0 + (float(abs(analyticConfig.max_threshold_percentage)) / float(100))
                else:
                    multiple = 1.0 - (float(abs(analyticConfig.max_threshold_percentage)) / float(100))
                currCondition = graphDF[col].astype(float) > (graphDF_avg[col].astype(float) * multiple)
                result_values = addCondition(analyticConfig.orCondition, result_values, currCondition)
                any_conditions_values = True

            if analyticConfig.average_min_threshold is not None:
                currCondition = graphDF_avg[col].astype(float) < analyticConfig.average_min_threshold
                result_average = addCondition(analyticConfig.orCondition, result_average, currCondition)
                any_conditions_average = True
            if analyticConfig.average_max_threshold is not None:
                currCondition = graphDF_avg[col].astype(float) > analyticConfig.average_max_threshold
                result_average = addCondition(analyticConfig.orCondition, result_average, currCondition)
                any_conditions_average = True

        # if orCondition is AND and no exceptional conditions have been found, then result_values will be all True
        if any_conditions_values == False:
            result_values = np.zeros(graphDF[col].shape, bool)
        exceptional_values = graphDF.loc[result_values, col]

        # if orCondition is AND and no exceptional conditions have been found, then result_average will be all True
        if any_conditions_average == False:
            result_average = np.zeros(graphDF_avg[col].shape, bool)
        exceptional_average = graphDF_avg.loc[result_average, col]

        # only keep alerts that are in consecutive periods of length analyticConfig.min_alert_minutes
        exceptional_values = keepConsecutiveAlerts(graphDF, exceptional_values, analyticConfig.min_alert_minutes)
        exceptional_average = keepConsecutiveAlerts(graphDF_avg, exceptional_average, analyticConfig.min_alert_minutes)

        # only evaluate the last analyticConfig.last_alert_minutes if set
        if analyticConfig.last_alert_minutes is not None:
            end = datetime.fromtimestamp(timelyMetric.timeDateRange.getEndMs() / 1000.00, DataOperations.utc)
            recentEnoughBegin = end - timedelta(minutes=analyticConfig.last_alert_minutes)
            exceptional_values = exceptional_values.ix[recentEnoughBegin:end]
            exceptional_average = exceptional_average.ix[recentEnoughBegin:end]

        anyValueExceptions = exceptional_values.size > 0
        anyAverageExceptions = exceptional_average.size > 0

        if (analyticConfig.display.lower() == "all") or (analyticConfig.display.lower() == "alerts" and anyValueExceptions):
            combined[col] = graphDF[col]

        if analyticConfig.rolling_average_samples is not None:
            if (analyticConfig.display.lower() == "all"):
                combined[col + '_avg'] = graphDF_avg[col]
            else:
                if (anyAverageExceptions):
                    combined[col + '_avg'] = graphDF_avg[col]
                if (anyValueExceptions and (analyticConfig.min_threshold_percentage is not None or analyticConfig.max_threshold_percentage is not None)):
                    combined[col + '_avg'] = graphDF_avg[col]

        if ((analyticConfig.display.lower() == "all") or (analyticConfig.display.lower() == "alerts" and anyValueExceptions)):
            combined[col + '_warn'] = exceptional_values.dropna()

            seriesConfig[col + '_warn'] = {
                "mode" : "markers",
                "marker" : {
                    "symbol" : "hash-open",
                    "color" : "red"
                }
            }

        if ((analyticConfig.display.lower() == "all") or (analyticConfig.display.lower() == "alerts" and anyAverageExceptions)):
            combined[col + '_avg_warn'] = exceptional_average.dropna()

            seriesConfig[col + '_avg_warn'] = {
                "mode" : "markers",
                "marker" : {
                    "symbol" : "hash-open",
                    "color" : "red"
                }
            }

    timelyAlert = None
    if not combined.empty:
        alertAnalyticConfig = TimelyAnalyticConfiguration(analyticConfig)
        if alertAnalyticConfig.groupByColumn is None:
            alertAnalyticConfig.groupByColumn = timelyMetric.metric + "_obs"
        combined = TimelyMetric.unpivot(combined, timelyMetric.metric, groupByColumn=alertAnalyticConfig.groupByColumn)
        combined = combined.sort_index()
        combined['date'] = combined.index.values
        combined = combined.sort_values(['date', alertAnalyticConfig.groupByColumn])
        combined = combined.drop(['date'], 1)
        combined = combined.dropna()
        combined = DataOperations.ensureMinSeriesLength(combined, alertAnalyticConfig.groupByColumn)

        message = DataOperations.getTitle(timelyMetric, analyticConfig, separator=', ')

        timelyAlert = TimelyAlert(timelyMetric, combined, message, seriesConfig, alertAnalyticConfig, notebook)

    return timelyAlert
