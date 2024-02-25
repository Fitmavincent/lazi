
START_SERVER='n'
END_SERVER='n'
DELETE_SERVER='n'
START_WEB_APP='n'

if [ $# -eq 0 ]
then
    echo "Please add command"
else
    for arg in "$@"
    do
        if [ "$arg" == '-up' ] || [ "$arg" == '-r' ]
        then
            START_SERVER='y'
            echo "Start Web Server"
        elif [ "$arg" == '-down' ] || [ "$arg" == '-d' ]
        then
            END_SERVER='y'
            echo "Stop Web Server"
        elif [ "$arg" == '-remove' ] || [ "$arg" == '-rm' ]
        then
            DELETE_SERVER='y'
            echo "Remove Web Server"
        elif [ "$arg" == '-front-end' ] || [ "$arg" == '-fe' ]
        then
            START_WEB_APP='y'
        fi
    done
fi

if [ $START_SERVER == 'y' ]
then
    docker-compose build --no-cache
    docker-compose up -d api
fi

if [ $END_SERVER == 'y' ]
then
    docker-compose stop
fi

if [ $DELETE_SERVER == 'y' ]
then
    docker-compose down --rmi all -v
fi

if [ $START_WEB_APP == 'y' ]
then
    echo "Not yet implemented."
fi