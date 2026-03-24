const { SNSClient, PublishCommand } = require('@aws-sdk/client-sns');

const snsClient = new SNSClient();

exports.handler = async (event) => {
    console.log('Received event:', JSON.stringify(event, null, 2));

    try {
        // CORS Preflight
        if (event.httpMethod === 'OPTIONS') {
            return {
                statusCode: 200,
                headers: getCorsHeaders(),
                body: ''
            };
        }

        const { url, urls, description } = JSON.parse(event.body || '{}');

        let urlList = [];
        if (urls && Array.isArray(urls)) {
            urlList = urls;
        } else if (url) {
            urlList = [url];
        }

        if (urlList.length === 0) {
            return {
                statusCode: 400,
                headers: getCorsHeaders(),
                body: JSON.stringify({ message: 'Missing url or urls in request body' })
            };
        }

        if (urlList.length > 50) {
            return {
                statusCode: 400,
                headers: getCorsHeaders(),
                body: JSON.stringify({ message: 'Too many URLs in a single request (max 50)' })
            };
        }

        const publishResults = [];
        const unhandledUrls = [];

        const topicArn = process.env.SNS_TOPIC_ARN;

        // Dispatch each URL in insertion order
        for (const u of urlList) {
            if (u.includes('radiko.jp')) {
                const result = await handleRadikoUrl(u, description, topicArn, snsClient);
                if (result) {
                    publishResults.push(result);
                } else {
                    unhandledUrls.push(u);
                }
            } else if (u.includes('tver.jp')) {
                const result = await handleTverUrl(u, topicArn, snsClient);
                publishResults.push(result);
            } else if (u.includes('youtube.com') || u.includes('youtu.be')) {
                const result = await handleYoutubeUrl(u, topicArn, snsClient);
                publishResults.push(result);
            } else {
                unhandledUrls.push(u);
            }
        }

        if (unhandledUrls.length > 0) {
            console.warn(`Unhandled URLs skipped: ${unhandledUrls.join(', ')}`);
        }

        if (publishResults.length === 0) {
            return {
                statusCode: 400,
                headers: getCorsHeaders(),
                body: JSON.stringify({ message: 'No valid URLs could be processed' })
            };
        }

        return {
            statusCode: 200,
            headers: getCorsHeaders(),
            body: JSON.stringify({
                message: 'Successfully published URL(s) to SNS',
                results: publishResults,
                unhandled: unhandledUrls
            })
        };

    } catch (error) {
        console.error('Error processing request:', error);
        return {
            statusCode: 500,
            headers: getCorsHeaders(),
            body: JSON.stringify({ message: 'Internal server error' })
        };
    }
};

function getCorsHeaders() {
    return {
        'Access-Control-Allow-Origin': '*',
        'Access-Control-Allow-Headers': 'Content-Type,X-Amz-Date,Authorization,X-Api-Key,X-Amz-Security-Token',
        'Access-Control-Allow-Methods': 'OPTIONS,POST'
    };
}

async function handleRadikoUrl(u, description, topicArn, snsClient) {
    const radikoRegex = /^https?:\/\/radiko\.jp\/#!\/ts\/([A-Za-z0-9_-]+)\/(\d{14})/;
    const podcastRegex = /^https?:\/\/radiko\.jp\/podcast\/episodes\//;

    if (podcastRegex.test(u)) {
        const payload = { type: 'radiko', url: u };
        if (description) payload.description = description;

        const params = {
            TopicArn: topicArn,
            Message: JSON.stringify(payload),
            Subject: 'Radiko Podcast Download Scheduled'
        };

        const result = await snsClient.send(new PublishCommand(params));
        console.log(`Successfully published Radiko podcast message ID: ${result.MessageId} for URL ${u}`);
        return { type: 'radiko_podcast', url: u, messageId: result.MessageId };
    }

    const match = u.match(radikoRegex);
    if (!match) {
        console.warn(`Skipped unparsable Radiko URL: ${u}`);
        return null;
    }

    const stationId = match[1];
    const startTime = match[2].substring(0, 12);
    const payload = { type: 'radiko', station_id: stationId, start_times: [startTime] };
    if (description) payload.description = description;

    const params = {
        TopicArn: topicArn,
        Message: JSON.stringify(payload),
        Subject: 'Radiko Recording Scheduled'
    };

    const result = await snsClient.send(new PublishCommand(params));
    console.log(`Successfully published Radiko message ID: ${result.MessageId} for station ${stationId} at ${startTime}`);
    return { type: 'radiko', stationId, startTime, messageId: result.MessageId };
}

async function handleTverUrl(u, topicArn, snsClient) {
    const payload = { type: 'tver', url: u };

    const params = {
        TopicArn: topicArn,
        Message: JSON.stringify(payload),
        Subject: 'TVer Recording Scheduled'
    };

    const result = await snsClient.send(new PublishCommand(params));
    console.log(`Successfully published TVer message ID: ${result.MessageId} for URL ${u}`);
    return { type: 'tver', url: u, messageId: result.MessageId };
}

async function handleYoutubeUrl(u, topicArn, snsClient) {
    const payload = { type: 'youtube', url: u };

    const params = {
        TopicArn: topicArn,
        Message: JSON.stringify(payload),
        Subject: 'YouTube Recording Scheduled'
    };

    const result = await snsClient.send(new PublishCommand(params));
    console.log(`Successfully published YouTube message ID: ${result.MessageId} for URL ${u}`);
    return { type: 'youtube', url: u, messageId: result.MessageId };
}
