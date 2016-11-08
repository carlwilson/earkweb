#!/usr/bin/env bash

echo "DIP PREPARE"
RESP=`curl -s -X POST -d '{"process_id": "c1b1c16e-2c00-474f-b99b-42019b3eaeed"}' http://localhost:8000/earkweb/search/prepareDIPWorkingArea`
echo $RESP
# status
sleep 3
JOBID=`echo $RESP | awk -F"," '{print $4}' | awk -F":" '{print $2}' | sed 's/ //g;s/\}//g;s/\"//g'`
echo `curl -s http://localhost:8000/earkweb/search/jobstatus/$JOBID`

echo "DIP CREATE"
RESP2=`curl -s -X POST -d '{"process_id": "c1b1c16e-2c00-474f-b99b-42019b3eaeed"}' http://localhost:8000/earkweb/search/createDIP`
echo $RESP2
# status
sleep 3
JOBID2=`echo $RESP2 | awk -F"," '{print $4}' | awk -F":" '{print $2}' | sed 's/ //g;s/\}//g;s/\"//g'`
echo `curl -s http://localhost:8000/earkweb/search/jobstatus/$JOBID2`
